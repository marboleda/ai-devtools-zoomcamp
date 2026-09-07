"""Django-Q2 task functions.

#19 enqueues ``process_meeting_record`` for every audio/video upload
(``projects.views.meeting_upload``); #20 (this file) implements the actual
pipeline: extract audio from video via ffmpeg when needed, transcribe via
OpenAI's ``gpt-4o-transcribe``, and write the result to
``MeetingRecord.transcript_text``.

As of #20's retroactive fix to #19 (see the comment on issue #20), a
transcript_file or pasted_text ``MeetingRecord`` is never enqueued at all —
both are written straight to ``processing_state=completed`` with
``transcript_text`` already populated at upload time. This task therefore
only ever expects to run for ``kind in (audio, video)``; see
``process_meeting_record``'s docstring for what happens if it's ever called
for anything else.
"""
import logging
import os
import subprocess
import tempfile

import openai

from .models import MeetingRecord

logger = logging.getLogger(__name__)

TRANSCRIPTION_MODEL = "gpt-4o-transcribe"


def _safe_remove(path):
    """Delete ``path`` if it's set, ignoring "already gone" and other
    OS-level removal errors — this is always called from a ``finally``
    block, and a failure to delete a temp file must never mask (or replace)
    whatever real success/failure state process_meeting_record already
    determined.
    """
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        logger.warning("Could not delete temp file %s", path, exc_info=True)


def process_meeting_record(meeting_record_id, temp_file_path):
    """Transcribe the audio/video upload backing ``meeting_record_id``,
    whose bytes were already written to ``temp_file_path`` by
    ``projects.views.meeting_upload`` before this job was enqueued (a
    background worker is a separate process from the request that received
    the upload, so the upload has to survive as a file on disk somewhere
    both can reach — see that view's own comment).

    If the record's ``kind`` is anything other than ``audio``/``video``,
    this task should never have been enqueued for it in the first place
    (#20's retroactive fix to #19 routes transcript_file/pasted_text
    straight to a completed state at upload time, with nothing to
    background-process) — this is treated as a safe no-op: it returns
    immediately without touching ``temp_file_path`` at all, in case
    something ever does call this directly for one of those records. The
    same applies if the record no longer exists.

    Otherwise:
    1. ``processing_state`` -> processing.
    2. For a video record, ffmpeg extracts the audio track to a second temp
       file; for audio, the uploaded file is used directly.
    3. The audio is sent to OpenAI's ``gpt-4o-transcribe`` API and the
       response text is written to ``transcript_text``, with
       ``processing_state`` -> completed.
    4. On any failure at any step (ffmpeg, the OpenAI call, file I/O, ...),
       ``processing_state`` -> failed with a human-readable ``error``
       message. No exception is re-raised, so django-q2 records this run as
       a normal (non-erroring) completion and never retries it — retrying
       would silently re-spend API cost on an upload that already failed
       once (#20's explicit decision).
    5. Every temp file this run touched — the original upload and, for
       video, the extracted-audio file — is deleted before returning,
       whether processing succeeded or failed.
    """
    try:
        record = MeetingRecord.objects.get(pk=meeting_record_id)
    except MeetingRecord.DoesNotExist:
        logger.error(
            "MeetingRecord %s no longer exists; discarding its temp file.",
            meeting_record_id,
        )
        _safe_remove(temp_file_path)
        return

    if record.kind not in (MeetingRecord.Kind.AUDIO, MeetingRecord.Kind.VIDEO):
        # Safe no-op — see docstring. Deliberately does not touch
        # temp_file_path: a transcript_file/pasted_text record never has one
        # written for it by meeting_upload, so there is nothing here that
        # this task owns or should delete.
        return

    record.processing_state = MeetingRecord.ProcessingState.PROCESSING
    record.save(update_fields=["processing_state"])

    extracted_audio_path = None
    try:
        if record.kind == MeetingRecord.Kind.VIDEO:
            # The output path is allocated before ffmpeg ever runs, so it's
            # already known to the outer `finally` below even if the ffmpeg
            # call itself raises — nothing leaks on a mid-extraction
            # failure.
            fd, extracted_audio_path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i", temp_file_path,
                    "-vn",
                    "-acodec", "libmp3lame",
                    extracted_audio_path,
                ],
                check=True,
                capture_output=True,
            )
            audio_path = extracted_audio_path
        else:
            audio_path = temp_file_path

        client = openai.OpenAI()
        with open(audio_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                file=audio_file, model=TRANSCRIPTION_MODEL
            )

        record.transcript_text = response.text
        record.processing_state = MeetingRecord.ProcessingState.COMPLETED
        record.save(update_fields=["transcript_text", "processing_state"])
    except Exception as exc:
        # Broad on purpose (per #20's constraints): an ffmpeg
        # CalledProcessError, any OpenAI SDK error, a file I/O error, or
        # anything else all land here and are handled terminally — nothing
        # is re-raised into django-q2's own retry machinery.
        logger.exception("Meeting record %s processing failed", meeting_record_id)
        record.processing_state = MeetingRecord.ProcessingState.FAILED
        record.error = f"Meeting transcription failed: {exc}"
        record.save(update_fields=["processing_state", "error"])
    finally:
        _safe_remove(temp_file_path)
        _safe_remove(extracted_audio_path)
