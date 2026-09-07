"""Django-Q2 task functions. #19 only needs to enqueue a job pointed at a
task function that exists — the actual transcription/processing pipeline
(streaming the upload to a temp file, running it through ffmpeg and OpenAI's
gpt-4o-transcribe, deleting the temp file, writing MeetingRecord.
transcript_text/processing_state/error) is #20's job, not this one's.
"""


def process_meeting_record(meeting_record_id):
    """Placeholder enqueued by projects.views.meeting_upload for every
    audio/video/transcript_file upload (#19). Not implemented here — #20
    will replace this body with the real pipeline. django-q2 does not
    validate a task's dotted path at enqueue time, only when a worker picks
    the job up, and no worker runs during the test suite, so this stub is
    never actually invoked by #19's own tests.
    """
    raise NotImplementedError("Meeting record processing is implemented in #20.")
