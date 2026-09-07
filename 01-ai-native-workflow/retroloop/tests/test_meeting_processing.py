"""Tests for #20: the background transcription pipeline.

``process_meeting_record`` is a Django-Q2 task, but since no worker runs
during the test suite (the same convention #19's own tests already used),
these tests call it directly as a plain function rather than going through
django-q2's queue/worker machinery. Both ``openai.OpenAI`` and the ffmpeg
subprocess call are mocked in every test — nothing here ever invokes a real
OpenAI API call or a real ffmpeg binary, even though ffmpeg happens to be on
this machine's PATH.

Mirrors tests/test_clustering.py for the mocked-external-API pattern, and
tests/test_meeting_upload.py for MeetingRecord/cycle fixture conventions.
"""
import os
import subprocess
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from projects.models import FeedbackCycle, MeetingRecord, Project
from projects.tasks import process_meeting_record

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class MeetingProcessingTestBase:
    def _make_cycle(self, django_user_model, suffix=""):
        owner = django_user_model.objects.create_user(
            username=f"proc_owner{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(name=f"Processing Project {suffix}", created_by=owner)
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.DISCUSSING
        )

    def _make_temp_file(self, content=b"fake media bytes", suffix=".mp3"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        return path

    def _mock_transcription_response(self, text):
        response = MagicMock()
        response.text = text
        return response


# -- Audio: the straightforward success and failure paths.


class TestProcessAudioRecord(MeetingProcessingTestBase):
    def test_successful_audio_transcription_completes_the_record_and_deletes_the_temp_file(
        self, django_user_model
    ):
        cycle = self._make_cycle(django_user_model, "1")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_path = self._make_temp_file()

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create.return_value = (
                self._mock_transcription_response("Alice: let's start the retro.")
            )
            mock_openai_cls.return_value = mock_client

            process_meeting_record(record.pk, temp_path)

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.transcript_text == "Alice: let's start the retro."
        assert record.error == ""

        # The required test: the temp file is gone once the job completes.
        assert not os.path.exists(temp_path)

        mock_client.audio.transcriptions.create.assert_called_once()
        _args, kwargs = mock_client.audio.transcriptions.create.call_args
        assert kwargs["model"] == "gpt-4o-transcribe"

    def test_sets_processing_state_to_processing_before_calling_openai(
        self, django_user_model
    ):
        cycle = self._make_cycle(django_user_model, "2")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_path = self._make_temp_file()

        def _assert_processing_and_raise(*_args, **_kwargs):
            mid_flight = MeetingRecord.objects.get(pk=record.pk)
            assert mid_flight.processing_state == MeetingRecord.ProcessingState.PROCESSING
            raise RuntimeError("simulated API failure")

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create.side_effect = _assert_processing_and_raise
            mock_openai_cls.return_value = mock_client

            process_meeting_record(record.pk, temp_path)

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.FAILED

    def test_openai_failure_marks_the_record_failed_with_a_message_and_deletes_the_temp_file(
        self, django_user_model
    ):
        cycle = self._make_cycle(django_user_model, "3")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        temp_path = self._make_temp_file()

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create.side_effect = RuntimeError(
                "The transcription API is down."
            )
            mock_openai_cls.return_value = mock_client

            # Must not raise — no automatic retry, per #20's decision.
            process_meeting_record(record.pk, temp_path)

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.FAILED
        assert "The transcription API is down." in record.error
        assert record.transcript_text == ""

        # Required test: the temp file is gone even on a simulated failure.
        assert not os.path.exists(temp_path)

    def test_missing_temp_file_is_handled_as_a_failure_not_a_crash(self, django_user_model):
        cycle = self._make_cycle(django_user_model, "4")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.AUDIO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        nonexistent_path = os.path.join(tempfile.gettempdir(), "does-not-exist-20.mp3")

        with patch("projects.tasks.openai.OpenAI"):
            process_meeting_record(record.pk, nonexistent_path)

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.FAILED
        assert record.error


# -- Video: the extra ffmpeg-extraction step.


class TestProcessVideoRecord(MeetingProcessingTestBase):
    def test_successful_video_transcription_runs_ffmpeg_then_completes_and_cleans_up(
        self, django_user_model
    ):
        cycle = self._make_cycle(django_user_model, "5")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.VIDEO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        video_path = self._make_temp_file(content=b"fake video bytes", suffix=".mp4")

        with patch("projects.tasks.subprocess.run") as mock_run, patch(
            "projects.tasks.openai.OpenAI"
        ) as mock_openai_cls:
            mock_client = MagicMock()
            mock_client.audio.transcriptions.create.return_value = (
                self._mock_transcription_response("Extracted-audio transcript.")
            )
            mock_openai_cls.return_value = mock_client

            process_meeting_record(record.pk, video_path)

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.transcript_text == "Extracted-audio transcript."

        # ffmpeg was actually invoked, against the video temp file, with an
        # output path distinct from it.
        mock_run.assert_called_once()
        (cmd,), run_kwargs = mock_run.call_args
        assert cmd[0] == "ffmpeg"
        assert video_path in cmd
        assert run_kwargs.get("check") is True
        extracted_audio_path = cmd[-1]
        assert extracted_audio_path != video_path

        # Required test: neither temp file (the original video, nor the
        # ffmpeg-extracted audio) exists once the job completes.
        assert not os.path.exists(video_path)
        assert not os.path.exists(extracted_audio_path)

    def test_ffmpeg_failure_marks_the_record_failed_and_deletes_both_temp_files(
        self, django_user_model
    ):
        cycle = self._make_cycle(django_user_model, "6")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.VIDEO,
            processing_state=MeetingRecord.ProcessingState.PENDING,
        )
        video_path = self._make_temp_file(content=b"fake video bytes", suffix=".mp4")

        created_paths = []
        real_mkstemp = tempfile.mkstemp

        def _tracking_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            created_paths.append(path)
            return fd, path

        with patch("projects.tasks.tempfile.mkstemp", side_effect=_tracking_mkstemp), patch(
            "projects.tasks.subprocess.run",
            side_effect=subprocess.CalledProcessError(1, ["ffmpeg"]),
        ), patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            # Must not raise — no automatic retry, per #20's decision.
            process_meeting_record(record.pk, video_path)

            # The OpenAI client should never even be constructed: ffmpeg
            # failed before transcription could start.
            mock_openai_cls.assert_not_called()

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.FAILED
        assert record.error
        assert record.transcript_text == ""

        # The extracted-audio temp path is allocated (via mkstemp) *before*
        # ffmpeg runs, so it exists even when ffmpeg itself fails — this
        # confirms it's still cleaned up rather than leaked.
        assert len(created_paths) == 1
        extracted_audio_path = created_paths[0]
        assert not os.path.exists(video_path)
        assert not os.path.exists(extracted_audio_path)


# -- transcript_file / pasted_text records are never picked up by this task.


class TestNonAudioVideoRecordsAreNeverProcessed(MeetingProcessingTestBase):
    def test_transcript_file_record_is_a_safe_no_op(self, django_user_model):
        # Per #20's retroactive fix to #19 (see the comment on issue #20),
        # a transcript_file record is never enqueued in the first place —
        # this only guards against something ever calling this task
        # directly for one anyway.
        cycle = self._make_cycle(django_user_model, "7")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.TRANSCRIPT_FILE,
            transcript_text="Already-uploaded transcript text.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        # A real, existing temp file stands in for "some path a caller
        # might mistakenly pass" — asserted still present afterwards to
        # confirm this task doesn't own or delete it.
        untouched_path = self._make_temp_file()

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            process_meeting_record(record.pk, untouched_path)
            mock_openai_cls.assert_not_called()

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.transcript_text == "Already-uploaded transcript text."
        assert os.path.exists(untouched_path)
        os.remove(untouched_path)

    def test_pasted_text_record_is_a_safe_no_op(self, django_user_model):
        cycle = self._make_cycle(django_user_model, "8")
        record = MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="Pasted transcript text.",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
        )
        untouched_path = self._make_temp_file()

        with patch("projects.tasks.openai.OpenAI") as mock_openai_cls:
            process_meeting_record(record.pk, untouched_path)
            mock_openai_cls.assert_not_called()

        record.refresh_from_db()
        assert record.processing_state == MeetingRecord.ProcessingState.COMPLETED
        assert record.transcript_text == "Pasted transcript text."
        assert os.path.exists(untouched_path)
        os.remove(untouched_path)

    def test_nonexistent_record_id_is_handled_without_crashing(self, django_user_model):
        # Not one of #20's own acceptance criteria, but a background task
        # can outlive the row it was enqueued for (e.g. deleted in the
        # meantime); this must not raise into django-q2, and should still
        # discard the temp file it was handed rather than leak it.
        temp_path = self._make_temp_file()

        process_meeting_record(999999999, temp_path)

        assert not os.path.exists(temp_path)
