"""#21: AI extraction of decisions and action items.

This call runs automatically as soon as a MeetingRecord's transcript is
ready — chained off #20's process_meeting_record success path (audio/video)
and off #19/#20's synchronous transcript_file/pasted_text completion in
projects.views.meeting_upload — never on a separate facilitator click. See
projects/tasks.py's ``extract_decisions_and_actions`` for the Django-Q2 task
wrapper and the three call sites that enqueue it.

The gating condition (per #21's own acceptance criteria) is checked here,
inside ``extract_decisions_and_actions_for_meeting_record``, rather than by
each caller: a MeetingRecord's ``transcript_text`` must be populated *and*
its cycle must have at least one ``DiscussionTopic`` with a non-blank
outcome (i.e. #18's discussion mode has actually resolved at least one
topic). If either is false, this function returns without calling the
Anthropic API at all — no wasted spend. In practice, plan.md's flow has
discussion happen before the meeting upload, but nothing in the code
enforces that ordering (a facilitator could technically upload a transcript
before running discussion mode, or discussion could still be in progress
with every topic still undecided), so this guard is load-bearing, not
theoretical.

Mirrors projects/clustering.py's structure and conventions closely: same
model id, the same forced tool-use pattern, and the same broad
exception-handling choice (log and give up gracefully) for the API call
itself, since a failed extraction should never crash the caller (the
Django-Q2 task, or — before #21's design decision to always enqueue — a
request/response cycle).
"""
import datetime
import logging

import anthropic

from .models import ActionItem, DecisionDraft, DiscussionTopic, DraftSource, Membership

logger = logging.getLogger(__name__)

EXTRACTION_MODEL = "claude-sonnet-5"

EXTRACT_DECISIONS_AND_ACTIONS_TOOL = {
    "name": "extract_decisions_and_actions",
    "description": (
        "Extract the decisions the team explicitly made and the action "
        "items the team explicitly agreed to during this retrospective "
        "meeting, based on the transcript and the list of discussion "
        "topics that were actually discussed. Also write a short summary "
        "of the meeting. Only extract what the transcript actually "
        "supports — never invent a decision, an action item, an owner, or "
        "a due date."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "decisions": {
                "type": "array",
                "description": "Decisions the team explicitly made during the meeting.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The decision, in one or two sentences.",
                        },
                    },
                    "required": ["text"],
                    "additionalProperties": False,
                },
            },
            "action_items": {
                "type": "array",
                "description": "Action items the team explicitly agreed to during the meeting.",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {
                            "type": "string",
                            "description": "What needs to be done.",
                        },
                        "owner_name": {
                            "type": ["string", "null"],
                            "description": (
                                "The name of the person who owns this action item, "
                                "exactly as it was said in the transcript, or null "
                                "if no owner was stated."
                            ),
                        },
                        "due_date": {
                            "type": ["string", "null"],
                            "description": (
                                "The due date in YYYY-MM-DD format, ONLY if a "
                                "specific date was explicitly stated in the "
                                "transcript. Leave this null if no due date was "
                                "mentioned for this action item — never infer or "
                                "guess a date."
                            ),
                        },
                    },
                    "required": ["description", "owner_name", "due_date"],
                    "additionalProperties": False,
                },
            },
            "summary": {
                "type": "string",
                "description": "A short (2-4 sentence) summary of the retrospective meeting.",
            },
        },
        "required": ["decisions", "action_items", "summary"],
        "additionalProperties": False,
    },
}


def _format_topics_for_prompt(topics):
    lines = []
    for topic in topics:
        line = f"- [{topic.get_outcome_display()}] {topic.cluster.name}"
        if topic.notes:
            line += f" — notes: {topic.notes}"
        lines.append(line)
    return "\n".join(lines)


def _parse_due_date(raw_due_date):
    """Parse the model's ``due_date`` string (expected ``YYYY-MM-DD``, per
    the tool schema's own instructions) into a ``datetime.date``. Returns
    ``None`` for a missing/blank value or anything that doesn't parse — a
    malformed date from the model is treated the same as no date at all,
    never a crash, and never a guess at what the model "must have meant".
    """
    if not raw_due_date:
        return None
    try:
        return datetime.datetime.strptime(raw_due_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Could not parse extracted due date %r; leaving it blank.", raw_due_date)
        return None


def _match_owner(owner_name, memberships):
    """Match ``owner_name`` (the model's free-text guess at who owns an
    action item) against ``memberships`` (the ``Membership`` rows for the
    record's cycle's project) by name, per stack.md: "Owners are matched
    against project membership; an unmatched name is left blank for the
    facilitator rather than guessed at."

    Tries, in order, an exact (case-insensitive) match against: username,
    then full name (``get_full_name()``), then first name. The first tier
    that yields exactly one candidate wins. A tier with zero matches falls
    through to the next; a tier with more than one match (e.g. two members
    named "Alex") is treated the same as no match at all — an ambiguous
    name is not guessed at either — and the search stops there rather than
    falling through to a looser tier that could resolve the ambiguity by
    accident.
    """
    name = (owner_name or "").strip()
    if not name:
        return None
    lowered = name.lower()

    for key in (
        lambda m: m.user.username,
        lambda m: m.user.get_full_name(),
        lambda m: m.user.first_name,
    ):
        candidates = [m for m in memberships if (key(m) or "").strip().lower() == lowered]
        if len(candidates) == 1:
            return candidates[0].user
        if len(candidates) > 1:
            return None

    return None


def extract_decisions_and_actions_for_meeting_record(record):
    """Ask the Anthropic API to extract decisions, action items, and a
    summary from ``record``'s transcript, and write the response as
    ``DecisionDraft``/``ActionItem`` rows (``source=DraftSource.AI``,
    ``confirmed_at=None``, ``confirmed_by=None``) plus
    ``record.extracted_summary``.

    Returns early, without calling the API, if either half of #21's gating
    condition isn't met yet: ``record.transcript_text`` is blank, or the
    record's cycle has no ``DiscussionTopic`` with a non-blank outcome.

    Any exception raised talking to the API (connection error, timeout, a
    malformed/unexpected response, ...) is caught here; on failure this
    function simply returns having created no drafts, the same
    log-and-give-up choice #14's suggest_clusters_for_cycle makes for its
    own API call.
    """
    if not record.transcript_text.strip():
        return

    cycle = record.cycle
    discussed_topics = list(
        DiscussionTopic.objects.for_cycle(cycle=cycle).exclude(outcome="").select_related("cluster")
    )
    if not discussed_topics:
        # #18's discussion mode hasn't resolved any topic yet — per #21's
        # own gating condition, skip the API call entirely rather than
        # spend on a transcript with nothing to extract against.
        return

    memberships = list(
        Membership.objects.filter(project=cycle.project).select_related("user")
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=4096,
            tools=[EXTRACT_DECISIONS_AND_ACTIONS_TOOL],
            tool_choice={"type": "tool", "name": "extract_decisions_and_actions"},
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Here is the transcript of a team retrospective "
                        "meeting, followed by the topics the team actually "
                        "discussed. Extract the decisions made and the "
                        "action items agreed to, and write a short "
                        "summary.\n\n"
                        f"Discussed topics:\n{_format_topics_for_prompt(discussed_topics)}\n\n"
                        f"Transcript:\n{record.transcript_text}"
                    ),
                }
            ],
        )
        tool_use_block = next(
            block for block in response.content if block.type == "tool_use"
        )
        result = tool_use_block.input
    except Exception:
        # Broad on purpose, per #21's own out-of-depth-detail call: any
        # Anthropic SDK error, timeout, or unexpected response shape all
        # land here and are handled terminally — nothing is re-raised into
        # the Django-Q2 task that called this.
        logger.exception(
            "AI decision/action-item extraction failed for meeting record %s",
            record.pk,
        )
        return

    for decision_data in result.get("decisions") or []:
        text = (decision_data.get("text") or "").strip()
        if not text:
            # An empty decision string contributes nothing rather than
            # creating a blank DecisionDraft row.
            continue
        DecisionDraft.objects.create(
            cycle=cycle,
            text=text,
            source=DraftSource.AI,
        )

    for action_data in result.get("action_items") or []:
        description = (action_data.get("description") or "").strip()
        if not description:
            continue
        ActionItem.objects.create(
            cycle=cycle,
            description=description,
            owner=_match_owner(action_data.get("owner_name"), memberships),
            due_date=_parse_due_date(action_data.get("due_date")),
            source=DraftSource.AI,
        )

    summary = (result.get("summary") or "").strip()
    if summary:
        record.extracted_summary = summary
        record.save(update_fields=["extracted_summary"])
