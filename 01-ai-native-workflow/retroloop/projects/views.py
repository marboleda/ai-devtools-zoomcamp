from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .clustering import suggest_clusters_for_cycle
from .decorators import facilitator_required
from .forms import CardEditForm, CardForm, JoinProjectForm, ProjectForm
from .models import (
    Card,
    CycleParticipation,
    FeedbackCycle,
    Membership,
    Project,
    generate_edit_token,
    hash_edit_token,
)

# Session key for the {card id (str): plaintext edit token} mapping kept for
# anonymous submissions in this session. Only the hash is ever persisted on
# the Card row (stack.md invariant #1); #9 reads this session entry to let
# an anonymous contributor edit their own card before the reveal.
ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY = "anonymous_card_edit_tokens"


def _anonymous_card_ids_in_session(request):
    """The card IDs (as ints) this session holds a plaintext edit token
    for, across every project/cycle the user has ever anonymously
    submitted to in this session. Callers narrow to one cycle themselves.
    """
    tokens = request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
    ids = []
    for raw_pk in tokens:
        try:
            ids.append(int(raw_pk))
        except (TypeError, ValueError):
            continue
    return ids


def _own_cards_for_cycle(request, cycle):
    # Delegates to Card.objects.visible_to (#10), the single reusable
    # visibility-scoped manager method: only request/session data is
    # touched here, and it's turned into the plain viewer/anonymous-ID
    # arguments that method takes, so the actual scoping logic lives in one
    # place instead of being re-derived per view.
    return Card.objects.visible_to(
        cycle=cycle,
        viewer=request.user,
        anonymous_card_ids=_anonymous_card_ids_in_session(request),
    )


def _assert_own_card(request, card):
    """Raise Http404 unless the current request is allowed to edit/withdraw
    ``card`` — the attributed author, or (for an anonymous card) a session
    holding the matching plaintext edit token. Mismatch and "doesn't exist"
    are both a plain Http404, so neither confirms the other case to the
    caller (same pattern as facilitator_required and create_card).
    """
    if card.author_id is not None:
        if card.author_id != request.user.id:
            raise Http404
        return

    tokens = request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
    token = tokens.get(str(card.pk))
    if not token or hash_edit_token(token) != card.edit_token_hash:
        raise Http404


def _get_own_card_or_404(request, project, card_id):
    card = get_object_or_404(
        Card.objects.select_related("cycle"), pk=card_id, cycle__project=project
    )
    _assert_own_card(request, card)
    return card


@login_required
def create_project(request):
    if request.method == "POST":
        form = ProjectForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.created_by = request.user
            project.save()
            Membership.objects.create(
                project=project,
                user=request.user,
                role=Membership.Role.FACILITATOR,
            )
            return redirect("project_detail", pk=project.pk)
    else:
        form = ProjectForm()

    return render(request, "projects/create.html", {"form": form})


@login_required
def project_detail(request, pk):
    # A single query keyed on (project, user) so a nonexistent project and an
    # existing project the user isn't a member of are indistinguishable: both
    # 404. This never confirms to a non-member whether a given project ID
    # even exists.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    memberships = (
        Membership.objects.filter(project=project)
        .select_related("user")
        .order_by("joined_at")
    )
    is_facilitator = membership.role == Membership.Role.FACILITATOR
    active_cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    # "N of M submitted" for #11: M is the project's member count, N is the
    # count of CycleParticipation rows for the active cycle. Never touches
    # Card, so card content/count is never exposed by this indicator.
    member_count = memberships.count()
    participation_count = (
        active_cycle.participations.count() if active_cycle is not None else None
    )

    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "memberships": memberships,
            "is_facilitator": is_facilitator,
            "active_cycle": active_cycle,
            "member_count": member_count,
            "participation_count": participation_count,
        },
    )


@login_required
def join_project(request):
    if request.method == "POST":
        form = JoinProjectForm(request.POST)
        if form.is_valid():
            code = form.cleaned_data["join_code"]
            try:
                project = Project.objects.get(join_code=code)
            except Project.DoesNotExist:
                form.add_error("join_code", "Invalid join code.")
            else:
                if Membership.objects.filter(project=project, user=request.user).exists():
                    form.add_error(None, "You're already a member of this project.")
                else:
                    Membership.objects.create(
                        project=project,
                        user=request.user,
                        role=Membership.Role.MEMBER,
                    )
                    return redirect("project_detail", pk=project.pk)
    else:
        form = JoinProjectForm()

    return render(request, "projects/join.html", {"form": form})


@facilitator_required
@require_POST
def create_cycle(request, pk, project, membership):
    # No manual input: the week label is auto-generated ("Cycle N",
    # incrementing per project) and every other field is either a default
    # or set by a later stage transition (#12, #17, #23).
    week = f"Cycle {project.cycles.count() + 1}"
    try:
        # A savepoint: on an IntegrityError from the partial unique
        # constraint (only one non-closed cycle per project) we need to
        # keep using the outer connection/transaction afterwards, both
        # here and in tests wrapped in their own atomic block.
        with transaction.atomic():
            FeedbackCycle.objects.create(
                project=project,
                week=week,
                state=FeedbackCycle.State.COLLECTING,
            )
    except IntegrityError:
        messages.error(
            request, "This project already has an active feedback cycle."
        )

    return redirect("project_detail", pk=project.pk)


@facilitator_required
@require_POST
def reveal_cycle(request, pk, project, membership):
    # Same "active cycle" lookup used by create_card: at most one non-closed
    # cycle can exist per project (DB-enforced), so there is never an
    # ambiguity about which cycle a bare "reveal" action targets.
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()

    # #12's own state check: whether the reveal is allowed is read from
    # FeedbackCycle.state, never inferred from revealed_at (which this
    # action itself is about to set). A double-click, a stale page, or an
    # attempt to reveal a cycle that hasn't started collecting yet all fail
    # here rather than mutating state a second time.
    if cycle is None or cycle.state != FeedbackCycle.State.COLLECTING:
        messages.error(
            request, "This cycle is not currently collecting submissions."
        )
        return redirect("project_detail", pk=project.pk)

    cycle.state = FeedbackCycle.State.REVEALED
    cycle.revealed_at = timezone.now()
    cycle.save(update_fields=["state", "revealed_at"])

    # #14: run once, immediately after reveal succeeds. Synchronous (no
    # Django-Q2) — that's reserved for the later meeting-transcription
    # pipeline (#20), not this call. A failed/timed-out API call is
    # swallowed inside suggest_clusters_for_cycle itself, so the cycle
    # stays "revealed" either way.
    suggest_clusters_for_cycle(cycle)

    messages.success(request, "Cards revealed.")
    return redirect("project_detail", pk=project.pk)


@login_required
def board_reveal(request, pk):
    # Same single-query, indistinguishable-404 membership lookup as
    # project_detail: any member (not just the facilitator) can view the
    # board, but a non-member — or a nonexistent project — gets a plain 404
    # per #5/#6's decision.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    # Same "active cycle" convention used throughout (create_card,
    # reveal_cycle): at most one non-closed cycle per project. Browsing a
    # past, closed cycle's board is out of scope for now — project_detail's
    # own "Previous retrospectives" section is still a placeholder.
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()

    # "Revealed or later" is read from revealed_at, not state, so this
    # covers revealed/clustering/voting/discussing alike. #14-#18's
    # clustering/voting/discussion UI is out of scope: every card is always
    # grouped by category only, never by cluster, regardless of how far the
    # cycle has actually progressed.
    revealed = cycle is not None and cycle.revealed_at is not None
    category_groups = None
    if revealed:
        # Post-reveal, visible_to returns every card in the cycle for any
        # viewer (#10) — the viewer/anonymous-ids arguments only matter
        # pre-reveal, which this branch never reaches.
        cards = list(Card.objects.visible_to(cycle=cycle, viewer=request.user))
        category_groups = [
            (value, label, [card for card in cards if card.category == value])
            for value, label in Card.Category.choices
        ]

    context = {
        "project": project,
        "cycle": cycle,
        "revealed": revealed,
        "category_groups": category_groups,
    }

    # htmx polls this same URL every ~3s (stack.md; the shared mechanism
    # #14-#18's later board modes reuse). A poll request only needs the
    # refreshed board content, not the surrounding page chrome/htmx script
    # tag, so an HX-Request carrying header gets just the fragment.
    template = (
        "projects/_board_reveal_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "projects/board_reveal.html"
    )
    return render(request, template, context)


@login_required
def create_card(request, pk):
    # Same single-query membership lookup as project_detail /
    # facilitator_required: a nonexistent project and an existing project
    # the user isn't a member of are indistinguishable, both 404, so the
    # route never confirms a project's existence to a non-member.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None:
        # No active cycle at all: the route is unreachable.
        raise Http404

    collecting = cycle.state == FeedbackCycle.State.COLLECTING
    form = None

    if request.method == "POST" and collecting:
        form = CardForm(request.POST)
        if form.is_valid():
            card = Card(
                cycle=cycle,
                category=form.cleaned_data["category"],
                text=form.cleaned_data["text"],
            )
            if form.cleaned_data["anonymous"]:
                token = generate_edit_token()
                card.author = None
                card.edit_token_hash = hash_edit_token(token)
            else:
                card.author = request.user
            card.save()

            # #11: participation is tracked per member (request.user), not
            # per card and not per Card.author — an anonymous submission
            # still records that this member submitted, without linking
            # them to the card. get_or_create relies on the unique
            # (cycle, member) constraint so a second card from the same
            # member in the same cycle never creates a second row.
            CycleParticipation.objects.get_or_create(cycle=cycle, member=request.user)

            if form.cleaned_data["anonymous"]:
                tokens = dict(
                    request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
                )
                tokens[str(card.pk)] = token
                request.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY] = tokens

            # Back to a cleared form, not project_detail, so submitting
            # several cards in a row doesn't require re-navigating.
            form = CardForm()
    elif collecting:
        form = CardForm()

    # Only while collecting (#9): once the cycle moves on, cards can no
    # longer be edited/withdrawn anyway, and pre-reveal visibility only
    # ever needs to include what you're allowed to act on right now.
    own_cards = _own_cards_for_cycle(request, cycle) if collecting else None

    return render(
        request,
        "projects/card_form.html",
        {
            "project": project,
            "cycle": cycle,
            "collecting": collecting,
            "form": form,
            "own_cards": own_cards,
        },
    )


@login_required
def edit_card(request, pk, card_id):
    # Same single-query, indistinguishable-404 membership lookup used by
    # create_card / project_detail.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    card = _get_own_card_or_404(request, project, card_id)

    if card.cycle.state != FeedbackCycle.State.COLLECTING:
        # Rejected for every card in the cycle once it's left "collecting",
        # even the owner's — #9's own rule, which #12's reveal depends on.
        # This is a normal, expected rejection (the owner already knows
        # their own card and the cycle's state), not a privacy leak, so it
        # gets a message + redirect rather than a 404.
        messages.error(request, "This cycle is no longer collecting submissions.")
        return redirect("create_card", pk=project.pk)

    if request.method == "POST":
        form = CardEditForm(request.POST)
        if form.is_valid():
            # Only category/text are touched — author and edit_token_hash
            # are never assigned here, so attribution cannot change as a
            # side effect of this view (see CardEditForm too).
            card.category = form.cleaned_data["category"]
            card.text = form.cleaned_data["text"]
            card.save(update_fields=["category", "text"])
            messages.success(request, "Card updated.")
            return redirect("create_card", pk=project.pk)
    else:
        form = CardEditForm(initial={"category": card.category, "text": card.text})

    return render(
        request,
        "projects/card_edit.html",
        {"project": project, "cycle": card.cycle, "card": card, "form": form},
    )


@login_required
@require_POST
def withdraw_card(request, pk, card_id):
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    card = _get_own_card_or_404(request, project, card_id)

    if card.cycle.state != FeedbackCycle.State.COLLECTING:
        messages.error(request, "This cycle is no longer collecting submissions.")
        return redirect("create_card", pk=project.pk)

    was_anonymous = card.author_id is None
    card.delete()

    if was_anonymous:
        # Drop the now-dangling token entry so a stale token for a deleted
        # card doesn't linger in the session.
        tokens = dict(request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {}))
        if tokens.pop(str(card_id), None) is not None:
            request.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY] = tokens

    messages.success(request, "Card withdrawn.")
    return redirect("create_card", pk=project.pk)
