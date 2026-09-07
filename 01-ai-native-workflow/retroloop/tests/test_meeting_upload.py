"""Tests for #19: the facilitator-only meeting upload page.

Submitting creates one MeetingRecord row for the project's active cycle,
with `kind` set to the input type. Pasted text has nothing to process (no
file, no background job) — transcript_text is populated directly and
processing_state goes straight to "completed". Every other kind
(audio/video/transcript_file) enqueues a Django-Q2 background job via
django_q.tasks.async_task, which is patched rather than allowed to actually
run (no worker runs during the test suite anyway).

Mirrors tests/test_reveal.py and tests/test_discussion.py for fixture and
naming conventions.
"""
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from projects.forms import MeetingUploadForm
from projects.models import FeedbackCycle, MeetingRecord, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


class _FakeUploadedFile:
    """A minimal stand-in for Django's UploadedFile, carrying only the two
    attributes MeetingUploadForm.clean() actually reads (``name``,
    ``size``). Used to unit-test the 500 MB cap without constructing a
    real ~500 MB payload, which client.post() would have to actually
    serialize and transmit.
    """

    def __init__(self, name, size):
        self.name = name
        self.size = size


@pytest.mark.django_db
class MeetingUploadTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"mfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Meeting Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"mmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_active_cycle(self, project, state=FeedbackCycle.State.DISCUSSING):
        return FeedbackCycle.objects.create(project=project, week="Cycle 1", state=state)

    def _upload_url(self, project):
        return reverse("meeting_upload", kwargs={"pk": project.pk})

    def _status_url(self, project):
        return reverse("meeting_upload_status", kwargs={"pk": project.pk})


# -- Form-level validation: file type and the 500 MB cap. Unit-tested
# directly against MeetingUploadForm so the size cap doesn't require
# constructing/transmitting a real ~500 MB payload.


class TestMeetingUploadForm:
    def test_accepts_an_mp3_as_audio(self):
        form = MeetingUploadForm(
            data={"pasted_text": ""},
            files={"file": _FakeUploadedFile("meeting.mp3", 1024)},
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["kind"] == MeetingRecord.Kind.AUDIO

    @pytest.mark.parametrize("name", ["meeting.wav", "meeting.m4a"])
    def test_accepts_other_audio_extensions(self, name):
        form = MeetingUploadForm(
            data={"pasted_text": ""}, files={"file": _FakeUploadedFile(name, 1024)}
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["kind"] == MeetingRecord.Kind.AUDIO

    @pytest.mark.parametrize("name", ["meeting.mp4", "meeting.webm", "meeting.mov"])
    def test_accepts_video_extensions(self, name):
        form = MeetingUploadForm(
            data={"pasted_text": ""}, files={"file": _FakeUploadedFile(name, 1024)}
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["kind"] == MeetingRecord.Kind.VIDEO

    @pytest.mark.parametrize("name", ["meeting.txt", "meeting.vtt", "meeting.srt"])
    def test_accepts_transcript_file_extensions(self, name):
        form = MeetingUploadForm(
            data={"pasted_text": ""}, files={"file": _FakeUploadedFile(name, 1024)}
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["kind"] == MeetingRecord.Kind.TRANSCRIPT_FILE

    @pytest.mark.parametrize("name", ["meeting.pdf", "meeting.exe", "meeting"])
    def test_rejects_unsupported_file_types(self, name):
        form = MeetingUploadForm(
            data={"pasted_text": ""}, files={"file": _FakeUploadedFile(name, 1024)}
        )
        assert not form.is_valid()

    def test_rejects_a_file_over_the_500mb_cap(self):
        form = MeetingUploadForm(
            data={"pasted_text": ""},
            files={"file": _FakeUploadedFile("meeting.mp3", 500 * 1024 * 1024 + 1)},
        )
        assert not form.is_valid()

    def test_accepts_a_file_exactly_at_the_500mb_cap(self):
        form = MeetingUploadForm(
            data={"pasted_text": ""},
            files={"file": _FakeUploadedFile("meeting.mp3", 500 * 1024 * 1024)},
        )
        assert form.is_valid(), form.errors

    def test_accepts_pasted_text_with_no_file(self):
        form = MeetingUploadForm(data={"pasted_text": "Some transcript text."}, files={})
        assert form.is_valid(), form.errors
        assert form.cleaned_data["kind"] == MeetingRecord.Kind.PASTED_TEXT
        assert form.cleaned_data["pasted_text"] == "Some transcript text."

    def test_rejects_neither_file_nor_pasted_text(self):
        form = MeetingUploadForm(data={"pasted_text": ""}, files={})
        assert not form.is_valid()

    def test_rejects_both_file_and_pasted_text(self):
        form = MeetingUploadForm(
            data={"pasted_text": "Some text"},
            files={"file": _FakeUploadedFile("meeting.mp3", 1024)},
        )
        assert not form.is_valid()

    def test_whitespace_only_pasted_text_is_treated_as_blank(self):
        form = MeetingUploadForm(data={"pasted_text": "   "}, files={})
        assert not form.is_valid()


# -- Facilitator-only access, per #6.


class TestMeetingUploadAccess(MeetingUploadTestBase):
    def test_facilitator_can_view_the_page(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._upload_url(project))

        assert response.status_code == 200
        assert "Meeting upload" in response.content.decode()

    def test_member_cannot_view_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.get(self._upload_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        self._make_active_cycle(project)

        response = client.get(self._upload_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_mu", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("meeting_upload", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_no_active_cycle_gets_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        client.force_login(facilitator)

        response = client.get(self._upload_url(project))

        assert response.status_code == 404

    def test_member_cannot_post_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.post(
            self._upload_url(project), {"pasted_text": "Sneaky submission"}
        )

        assert response.status_code == 404
        assert not MeetingRecord.objects.exists()


# -- The explicitly required test: pasted text vs. audio upload.


class TestMeetingUploadSubmission(MeetingUploadTestBase):
    def test_pasted_text_creates_a_completed_record_with_no_job_enqueued(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)

        with patch("projects.views.async_task") as mock_async_task:
            response = client.post(
                self._upload_url(project),
                {"pasted_text": "Alice: let's start. Bob: sounds good."},
            )

        assert response.status_code == 302
        mock_async_task.assert_not_called()

        record = MeetingRecord.objects.get(cycle=cycle)
        assert record.kind == MeetingRecord.Kind.PASTED_TEXT
        assert record.transcript_text == "Alice: let's start. Bob: sounds good."
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.task_id == ""

    def test_audio_upload_creates_a_pending_record_with_a_job_enqueued(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "standup.mp3", b"fake audio bytes", content_type="audio/mpeg"
        )

        with patch("projects.views.async_task", return_value="task-123") as mock_async_task:
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        mock_async_task.assert_called_once()
        args, _kwargs = mock_async_task.call_args
        assert args[0] == "projects.tasks.process_meeting_record"

        record = MeetingRecord.objects.get(cycle=cycle)
        assert record.kind == MeetingRecord.Kind.AUDIO
        assert record.transcript_text == ""
        assert record.processing_state == MeetingRecord.ProcessingState.PENDING
        assert record.task_id == "task-123"
        # The record's own pk is what got enqueued, so #20's worker can look
        # this exact row up.
        assert args[1] == record.pk

    def test_video_upload_creates_a_pending_record_with_a_job_enqueued(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)
        upload = SimpleUploadedFile("standup.mp4", b"fake video bytes", content_type="video/mp4")

        with patch("projects.views.async_task", return_value="task-456"):
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        record = MeetingRecord.objects.get(cycle=cycle)
        assert record.kind == MeetingRecord.Kind.VIDEO
        assert record.processing_state == MeetingRecord.ProcessingState.PENDING

    def test_transcript_file_upload_creates_a_pending_record_with_a_job_enqueued(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "standup.vtt", b"WEBVTT\n\nfake transcript", content_type="text/vtt"
        )

        with patch("projects.views.async_task", return_value="task-789"):
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        record = MeetingRecord.objects.get(cycle=cycle)
        assert record.kind == MeetingRecord.Kind.TRANSCRIPT_FILE
        assert record.processing_state == MeetingRecord.ProcessingState.PENDING

    def test_unsupported_file_type_is_rejected_before_any_record_is_created(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "notes.pdf", b"%PDF-1.4 fake", content_type="application/pdf"
        )

        with patch("projects.views.async_task") as mock_async_task:
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 200
        assert not MeetingRecord.objects.exists()
        mock_async_task.assert_not_called()

    def test_neither_file_nor_text_is_rejected_without_crashing(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._upload_url(project), {"pasted_text": ""})

        assert response.status_code == 200
        assert not MeetingRecord.objects.exists()


# -- Only one MeetingRecord can be actively processing per cycle at a time.


class TestOneActiveProcessingRecordPerCycle(MeetingUploadTestBase):
    def test_second_audio_upload_while_one_is_pending_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "second.mp3", b"more fake audio", content_type="audio/mpeg"
        )

        with patch("projects.views.async_task") as mock_async_task:
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 200
        assert "already being processed" in response.content.decode()
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 1
        mock_async_task.assert_not_called()

    def test_second_upload_while_one_is_processing_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "13"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.VIDEO,
            processing_state=MeetingRecord.ProcessingState.PROCESSING,
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "second.mp4", b"more fake video", content_type="video/mp4"
        )

        response = client.post(
            self._upload_url(project), {"pasted_text": "", "file": upload}
        )

        assert response.status_code == 200
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 1

    def test_upload_is_allowed_once_the_prior_record_completed(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "14"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "second.mp3", b"more fake audio", content_type="audio/mpeg"
        )

        with patch("projects.views.async_task", return_value="task-xyz"):
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 2

    def test_upload_is_allowed_once_the_prior_record_failed(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "15"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.FAILED,
            error="Transcription API timed out.",
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "second.mp3", b"more fake audio", content_type="audio/mpeg"
        )

        with patch("projects.views.async_task", return_value="task-xyz"):
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 2

    def test_pasted_text_is_never_blocked_by_an_active_processing_record(
        self, client, django_user_model
    ):
        # Pasted text has nothing to process, so it should never contend
        # with an in-flight audio/video/transcript_file job — per #19's own
        # note that pasted text "never blocks a subsequent upload".
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "16"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        client.force_login(facilitator)

        response = client.post(
            self._upload_url(project), {"pasted_text": "A pasted transcript."}
        )

        assert response.status_code == 302
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 2
        pasted = MeetingRecord.objects.get(cycle=cycle, kind=MeetingRecord.Kind.PASTED_TEXT)
        assert pasted.processing_state == MeetingRecord.ProcessingState.COMPLETED

    def test_a_completed_pasted_text_record_never_blocks_a_later_upload(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "17"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Earlier notes.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        client.force_login(facilitator)
        upload = SimpleUploadedFile(
            "later.mp3", b"more fake audio", content_type="audio/mpeg"
        )

        with patch("projects.views.async_task", return_value="task-abc"):
            response = client.post(
                self._upload_url(project), {"pasted_text": "", "file": upload}
            )

        assert response.status_code == 302
        assert MeetingRecord.objects.filter(cycle=cycle).count() == 2


# -- Processing-status indicator: the ~3s htmx-polled fragment.


class TestMeetingUploadStatus(MeetingUploadTestBase):
    def test_page_carries_htmx_polling_markup_for_the_status_fragment(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "18"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._upload_url(project))
        content = response.content.decode()

        assert 'hx-trigger="every 3s"' in content
        assert f'hx-get="{self._status_url(project)}"' in content

    def test_status_endpoint_reflects_processing_state(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "19"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PROCESSING,
        )
        client.force_login(facilitator)

        response = client.get(self._status_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Processing" in content

    def test_status_endpoint_is_facilitator_only(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.get(self._status_url(project))

        assert response.status_code == 404

    def test_no_records_yet_shows_a_friendly_message(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._status_url(project))
        content = response.content.decode()

        assert "No meeting record uploaded yet." in content

    def test_error_is_shown_for_a_failed_record(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_active_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.FAILED,
            error="Transcription API timed out.",
        )
        client.force_login(facilitator)

        response = client.get(self._status_url(project))
        content = response.content.decode()

        assert "Transcription API timed out." in content


# -- MeetingRecordQuerySet.active_processing_for_cycle, called directly —
# the same "test the manager method" convention #16/#18 use for their own
# query-layer helpers.


@pytest.mark.django_db
class TestActiveProcessingForCycleQuerySet:
    def test_returns_pending_and_processing_but_not_completed_failed_or_pasted_text(
        self, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="qs_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="QS Project", created_by=owner)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        pending = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        processing = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.VIDEO,
            processing_state=MeetingRecord.ProcessingState.PROCESSING,
        )
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.FAILED,
        )
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Text",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )

        active = MeetingRecord.objects.active_processing_for_cycle(cycle=cycle)

        assert set(active) == {pending, processing}

    def test_scoped_to_the_given_cycle_only(self, django_user_model):
        owner = django_user_model.objects.create_user(
            username="qs_owner2", password=VALID_PASSWORD
        )
        project_a = Project.objects.create(name="QS Project A", created_by=owner)
        project_b = Project.objects.create(name="QS Project B", created_by=owner)
        cycle_a = FeedbackCycle.objects.create(
            project=project_a, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        cycle_b = FeedbackCycle.objects.create(
            project=project_b, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )
        MeetingRecord.objects.create(
            cycle=cycle_b,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )

        active = MeetingRecord.objects.active_processing_for_cycle(cycle=cycle_a)

        assert list(active) == []
