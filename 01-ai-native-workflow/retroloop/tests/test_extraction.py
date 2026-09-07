"""Tests for #21: AI extraction of decisions and action items.

projects.extraction.extract_decisions_and_actions_for_meeting_record is the
unit under test for most of this file — these tests never call the real
Anthropic API: ``anthropic.Anthropic`` is mocked exactly the way
tests/test_clustering.py mocks it (patch the class, set
``.messages.create.return_value``/``.side_effect`` on a MagicMock). No
ANTHROPIC_API_KEY is required to run this suite.

projects.tasks.extract_decisions_and_actions (the Django-Q2 task wrapper)
is called directly as a plain function, the same convention
tests/test_meeting_processing.py already uses for process_meeting_record —
no worker runs during the test suite.

Finally, a few integration tests confirm the three enqueue call sites named
in #21's own implementation guidance actually enqueue this task: #20's
process_meeting_record success path, and both the pasted_text and
transcript_file branches of projects.views.meeting_upload (the third,
audio/video-at-upload-time, is covered by tests/test_meeting_upload.py's
own updated assertions instead, since that branch's job is
process_meeting_record, not this one).
"""
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from projects.extraction import extract_decisions_and_actions_for_meeting_record
from projects.models import (
    ActionItem,
    Cluster,
    DecisionDraft,
    DiscussionTopic,
    FeedbackCycle,
    MeetingRecord,
    Membership,
    Project,
)
from projects.tasks import extract_decisions_and_actions, process_meeting_record

VALID_PASSWORD = "correct horse battery staple"


def _make_extraction_response(decisions=None, action_items=None, summary="A short summary."):
    """Build a fake anthropic.Anthropic().messages.create(...) return value
    carrying a single tool_use content block, shaped like the real SDK's
    parsed response (a ``.content`` list of blocks, each with ``.type`` and
    ``.input``) — same convention as tests/test_clustering.py's own
    ``_make_tool_use_response``.
    """
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.input = {
        "decisions": decisions or [],
        "action_items": action_items or [],
        "summary": summary,
    }
    response = MagicMock()
    response.content = [tool_use_block]
    return response


@pytest.mark.django_db
class ExtractionTestBase:
    def _make_cycle_with_discussed_topic(
        self, django_user_model, suffix="", outcome=DiscussionTopic.Outcome.DISCUSSED
    ):
        facilitator = django_user_model.objects.create_user(
            username=f"extfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Extraction Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        cluster = Cluster.objects.create(
            cycle=cycle, name="Process issues", origin=Cluster.Origin.SUGGESTED
        )
        topic = DiscussionTopic.objects.create(cluster=cluster, rank=1, outcome=outcome)
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Alice: let's ship the fix by Friday. Bob: I'll own it.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        return facilitator, project, cycle, topic, record

    def _add_member(self, project, username, first_name="", last_name=""):
        user_model = project.created_by.__class__
        user = user_model.objects.create_user(
            username=username,
            password=VALID_PASSWORD,
            first_name=first_name,
            last_name=last_name,
        )
        Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
        return user


# -- #21's own gating condition: transcript_text populated AND the cycle has
# at least one DiscussionTopic with a non-blank outcome. Neither half being
# true should ever result in an API call.


class TestGating(ExtractionTestBase):
    def test_blank_transcript_text_skips_the_api_call(self, django_user_model):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "1"
        )
        record.transcript_text = ""
        record.save(update_fields=["transcript_text"])

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            extract_decisions_and_actions_for_meeting_record(record)
            mock_anthropic_cls.assert_not_called()

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0
        assert ActionItem.objects.filter(cycle=cycle).count() == 0

    def test_whitespace_only_transcript_text_skips_the_api_call(self, django_user_model):
        _facilitator, _project, _cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "1b"
        )
        record.transcript_text = "   \n  "
        record.save(update_fields=["transcript_text"])

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            extract_decisions_and_actions_for_meeting_record(record)
            mock_anthropic_cls.assert_not_called()

    def test_no_discussion_topics_at_all_skips_the_api_call(self, django_user_model):
        facilitator = django_user_model.objects.create_user(
            username="extfac2", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Extraction Project 2", created_by=facilitator)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Some transcript with no discussion topics yet.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            extract_decisions_and_actions_for_meeting_record(record)
            mock_anthropic_cls.assert_not_called()

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0
        assert ActionItem.objects.filter(cycle=cycle).count() == 0

    def test_topics_with_no_outcome_set_yet_skip_the_api_call(self, django_user_model):
        # Discussion mode (#18) has started (a DiscussionTopic row exists)
        # but the facilitator hasn't marked anything discussed/skipped/
        # deferred yet — outcome is still blank. This is the scenario
        # #21's own guidance calls out: a facilitator could technically
        # upload a transcript before (or during) discussion mode, and
        # nothing in the code stops them.
        _facilitator, _project, cycle, topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "3", outcome=""
        )
        assert topic.outcome == ""

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            extract_decisions_and_actions_for_meeting_record(record)
            mock_anthropic_cls.assert_not_called()

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0

    @pytest.mark.parametrize(
        "outcome",
        [
            DiscussionTopic.Outcome.DISCUSSED,
            DiscussionTopic.Outcome.SKIPPED,
            DiscussionTopic.Outcome.DEFERRED,
        ],
    )
    def test_any_non_blank_outcome_satisfies_the_gate(self, django_user_model, outcome):
        # "an outcome set" per the issue's own wording covers all three
        # Outcome values, not just "discussed" — skipped/deferred still
        # count, since the facilitator has made a call on that topic
        # either way.
        _facilitator, _project, _cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, f"4{outcome}", outcome=outcome
        )
        fake_response = _make_extraction_response()

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

            mock_client.messages.create.assert_called_once()


# -- The successful path: rows are created correctly, with the invariants
# the issue's own required test calls out.


class TestSuccessfulExtraction(ExtractionTestBase):
    def test_creates_decision_drafts_and_action_items_with_confirmed_at_null(
        self, django_user_model
    ):
        _facilitator, project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "10"
        )
        owner = self._add_member(project, "bob")
        fake_response = _make_extraction_response(
            decisions=[{"text": "Ship the fix on Friday."}],
            action_items=[
                {
                    "description": "Fix the flaky test.",
                    "owner_name": "bob",
                    "due_date": "2026-09-12",
                }
            ],
            summary="The team decided to ship the fix; Bob owns the flaky test.",
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        decision = DecisionDraft.objects.get(cycle=cycle)
        assert decision.text == "Ship the fix on Friday."
        assert decision.source == "ai"
        # The explicitly required test: confirmed_at is NULL on everything
        # this call creates.
        assert decision.confirmed_at is None
        assert decision.confirmed_by is None

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.description == "Fix the flaky test."
        assert action_item.source == "ai"
        assert action_item.confirmed_at is None
        assert action_item.confirmed_by is None
        assert action_item.owner == owner
        assert action_item.due_date.isoformat() == "2026-09-12"
        assert action_item.status == ActionItem.Status.OPEN

        record.refresh_from_db()
        assert record.extracted_summary == (
            "The team decided to ship the fix; Bob owns the flaky test."
        )

        # Forced tool-use, on the pinned model, per stack.md / #21's own
        # constraints.
        _args, kwargs = mock_client.messages.create.call_args
        assert kwargs["model"] == "claude-sonnet-5"
        assert kwargs["tool_choice"] == {
            "type": "tool",
            "name": "extract_decisions_and_actions",
        }
        assert kwargs["tools"][0]["name"] == "extract_decisions_and_actions"
        assert kwargs["tools"][0]["strict"] is True

    def test_unmatched_owner_name_is_left_blank_not_guessed(self, django_user_model):
        # The explicitly required test, other half: an owner name with no
        # membership match leaves owner blank rather than raising an error
        # or guessing.
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "11"
        )
        fake_response = _make_extraction_response(
            action_items=[
                {
                    "description": "Investigate the outage.",
                    "owner_name": "Nobody On This Project",
                    "due_date": None,
                }
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            # Must not raise.
            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.owner is None
        assert action_item.due_date is None

    def test_no_owner_stated_leaves_owner_blank(self, django_user_model):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "12"
        )
        fake_response = _make_extraction_response(
            action_items=[
                {"description": "Someone should look at this.", "owner_name": None, "due_date": None}
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.owner is None

    def test_owner_matched_case_insensitively_by_username(self, django_user_model):
        _facilitator, project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "13"
        )
        owner = self._add_member(project, "Carla")
        fake_response = _make_extraction_response(
            action_items=[
                {"description": "Update the docs.", "owner_name": "carla", "due_date": None}
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.owner == owner

    def test_owner_matched_by_full_name_when_username_does_not_match(self, django_user_model):
        _facilitator, project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "14"
        )
        owner = self._add_member(project, "dwhitfield", first_name="Dana", last_name="Whitfield")
        fake_response = _make_extraction_response(
            action_items=[
                {
                    "description": "Send the follow-up email.",
                    "owner_name": "Dana Whitfield",
                    "due_date": None,
                }
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.owner == owner

    def test_ambiguous_name_shared_by_two_members_is_left_blank(self, django_user_model):
        _facilitator, project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "15"
        )
        self._add_member(project, "alex_one", first_name="Alex")
        self._add_member(project, "alex_two", first_name="Alex")
        fake_response = _make_extraction_response(
            action_items=[
                {"description": "Review the PR.", "owner_name": "Alex", "due_date": None}
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.owner is None

    def test_malformed_due_date_from_the_model_is_left_blank_not_a_crash(
        self, django_user_model
    ):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "16"
        )
        fake_response = _make_extraction_response(
            action_items=[
                {
                    "description": "Do the thing.",
                    "owner_name": None,
                    "due_date": "next Friday",
                }
            ]
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            # Must not raise.
            extract_decisions_and_actions_for_meeting_record(record)

        action_item = ActionItem.objects.get(cycle=cycle)
        assert action_item.due_date is None

    def test_blank_decision_text_and_blank_action_description_are_skipped(
        self, django_user_model
    ):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "17"
        )
        fake_response = _make_extraction_response(
            decisions=[{"text": "   "}],
            action_items=[{"description": "", "owner_name": None, "due_date": None}],
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0
        assert ActionItem.objects.filter(cycle=cycle).count() == 0

    def test_blank_summary_leaves_extracted_summary_untouched(self, django_user_model):
        _facilitator, _project, _cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "18"
        )
        fake_response = _make_extraction_response(summary="   ")

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        record.refresh_from_db()
        assert record.extracted_summary == ""


# -- The API call itself can fail; this must never crash the caller.


class TestFailedExtraction(ExtractionTestBase):
    def test_connection_error_creates_nothing_and_does_not_raise(self, django_user_model):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "20"
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            mock_client.messages.create.side_effect = anthropic.APIConnectionError(
                request=request
            )
            mock_anthropic_cls.return_value = mock_client

            # Must not raise.
            extract_decisions_and_actions_for_meeting_record(record)

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0
        assert ActionItem.objects.filter(cycle=cycle).count() == 0
        record.refresh_from_db()
        assert record.extracted_summary == ""

    def test_timeout_also_creates_nothing(self, django_user_model):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "21"
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            mock_client.messages.create.side_effect = anthropic.APITimeoutError(request)
            mock_anthropic_cls.return_value = mock_client

            extract_decisions_and_actions_for_meeting_record(record)

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0

    def test_response_with_no_tool_use_block_is_handled_gracefully(self, django_user_model):
        _facilitator, _project, cycle, _topic, record = self._make_cycle_with_discussed_topic(
            django_user_model, "22"
        )

        with patch("projects.extraction.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            text_block = MagicMock()
            text_block.type = "text"
            response = MagicMock()
            response.content = [text_block]
            mock_client.messages.create.return_value = response
            mock_anthropic_cls.return_value = mock_client

            # Must not raise even though there's no tool_use block to find.
            extract_decisions_and_actions_for_meeting_record(record)

        assert DecisionDraft.objects.filter(cycle=cycle).count() == 0


# -- projects.tasks.extract_decisions_and_actions: the Django-Q2 task
# wrapper, called directly as a plain function (no worker runs during the
# test suite), same convention as tests/test_meeting_processing.py.


@pytest.mark.django_db
class TestExtractDecisionsAndActionsTask:
    def test_delegates_to_the_extraction_function_for_an_existing_record(
        self, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="task_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Task Project", created_by=owner)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Some transcript.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )

        with patch(
            "projects.tasks.extract_decisions_and_actions_for_meeting_record"
        ) as mock_extract:
            extract_decisions_and_actions(record.pk)

        mock_extract.assert_called_once()
        (called_record,), _kwargs = mock_extract.call_args
        assert called_record.pk == record.pk

    def test_nonexistent_record_id_is_handled_without_crashing(self):
        with patch(
            "projects.tasks.extract_decisions_and_actions_for_meeting_record"
        ) as mock_extract:
            # Must not raise.
            extract_decisions_and_actions(999999999)
            mock_extract.assert_not_called()


# -- Integration: the enqueue wiring itself. #21's own decision was to
# always enqueue this task rather than ever call it inline, from three call
# sites: process_meeting_record's success path (audio/video), and both the
# pasted_text and transcript_file branches of meeting_upload (the third of
# those two lives in tests/test_meeting_upload.py's own updated
# assertions).


@pytest.mark.django_db
class TestProcessMeetingRecordEnqueuesExtraction:
    def _make_cycle(self, django_user_model, suffix=""):
        owner = django_user_model.objects.create_user(
            username=f"pmr_owner{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(name=f"PMR Project {suffix}", created_by=owner)
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )

    def test_successful_transcription_enqueues_extraction_against_the_same_record(
        self, django_user_model, tmp_path
    ):
        cycle = self._make_cycle(django_user_model, "1")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_file = tmp_path / "audio.mp3"
        temp_file.write_bytes(b"fake audio bytes")

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls, patch(
            "projects.tasks.async_task"
        ) as mock_async_task:
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.text = "Transcribed text."
            mock_client.audio.transcriptions.create.return_value = mock_response
            mock_openai_cls.return_value = mock_client

            process_meeting_record(record.pk, str(temp_file))

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        mock_async_task.assert_called_once_with(
            "projects.tasks.extract_decisions_and_actions", record.pk
        )

    def test_failed_transcription_never_enqueues_extraction(self, django_user_model, tmp_path):
        cycle = self._make_cycle(django_user_model, "2")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_file = tmp_path / "audio.mp3"
        temp_file.write_bytes(b"fake audio bytes")

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls, patch(
            "projects.tasks.async_task"
        ) as mock_async_task:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create.side_effect = RuntimeError("API down")
            mock_openai_cls.return_value = mock_client

            process_meeting_record(record.pk, str(temp_file))

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.FAILED
        mock_async_task.assert_not_called()

    def test_a_failure_to_enqueue_extraction_does_not_turn_a_successful_transcription_into_failed(
        self, django_user_model, tmp_path
    ):
        cycle = self._make_cycle(django_user_model, "3")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_file = tmp_path / "audio.mp3"
        temp_file.write_bytes(b"fake audio bytes")

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls, patch(
            "projects.tasks.async_task", side_effect=RuntimeError("broker unavailable")
        ):
            mock_client = MagicMock()
            mock_response = MagicMock()
            mock_response.text = "Transcribed text."
            mock_client.audio.transcriptions.create.return_value = mock_response
            mock_openai_cls.return_value = mock_client

            # Must not raise.
            process_meeting_record(record.pk, str(temp_file))

        record.refresh_from_db()
        # The transcription itself already succeeded and was already saved
        # before the enqueue was attempted — a broker problem enqueuing
        # extraction must never retroactively mark this FAILED.
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.transcript_text == "Transcribed text."


@pytest.mark.django_db
class TestMeetingUploadEnqueuesExtraction:
    def _make_project_with_facilitator(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"mu_ext_fac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"MU Extraction Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        return facilitator, project

    def test_pasted_text_upload_enqueues_extraction_against_the_new_record(
        self, client, django_user_model
    ):
        facilitator, project = self._make_project_with_facilitator(django_user_model, "1")
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        client.force_login(facilitator)

        with patch("projects.views.async_task") as mock_async_task:
            response = client.post(
                reverse("meeting_upload", kwargs={"pk": project.pk}),
                {"pasted_text": "Alice: let's ship it."},
            )

        assert response.status_code == 302
        record = MeetingRecord.objects.get(cycle=cycle)
        mock_async_task.assert_called_once_with(
            "projects.tasks.extract_decisions_and_actions", record.pk
        )

    def test_transcript_file_upload_enqueues_extraction_against_the_new_record(
        self, client, django_user_model
    ):
        facilitator, project = self._make_project_with_facilitator(django_user_model, "2")
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "standup.txt", b"A plain text transcript.", content_type="text/plain"
        )

        with patch("projects.views.async_task") as mock_async_task:
            response = client.post(
                reverse("meeting_upload", kwargs={"pk": project.pk}),
                {"pasted_text": "", "file": upload},
            )

        assert response.status_code == 302
        record = MeetingRecord.objects.get(cycle=cycle)
        mock_async_task.assert_called_once_with(
            "projects.tasks.extract_decisions_and_actions", record.pk
        )
