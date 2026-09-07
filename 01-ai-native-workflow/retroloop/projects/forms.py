from django import forms

from .models import Card, Cluster, MeetingRecord, Project


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


class JoinProjectForm(forms.Form):
    join_code = forms.CharField(label="Join code")

    def clean_join_code(self):
        # People copy-paste codes from a shared link/screen or retype them
        # from a screenshot, so matching is case-insensitive and tolerant of
        # stray whitespace — normalize here before the view does the lookup.
        return self.cleaned_data["join_code"].strip().upper()


class CardForm(forms.Form):
    category = forms.ChoiceField(choices=Card.Category.choices)
    # CharField strips leading/trailing whitespace by default before
    # validating, so a whitespace-only submission is treated the same as a
    # blank one and rejected by `required=True` below — no extra `clean_`
    # needed for that rule. `max_length` rejects over-limit text with a
    # validation error instead of silently truncating it.
    text = forms.CharField(max_length=280, widget=forms.Textarea(attrs={"rows": 2}))
    anonymous = forms.BooleanField(required=False, label="Submit anonymously")


class CardEditForm(CardForm):
    """Same category/text validation as CardForm, with the "submit
    anonymously" control removed. Editing never changes a card's
    attribution (#9) — there is no field here that could change it, by
    construction, not just by convention in the view.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        del self.fields["anonymous"]


class ClusterNameForm(forms.ModelForm):
    """Validates a cluster rename (#15): non-blank (CharField strips
    whitespace before validating, same as CardForm), capped at Cluster.name's
    own max_length. Works identically regardless of the cluster's origin —
    this form never touches that field.
    """

    class Meta:
        model = Cluster
        fields = ["name"]


class DiscussionNoteForm(forms.Form):
    """Validates one free-text note (#18) before it gets appended to
    ``DiscussionTopic.notes`` — non-blank (CharField strips whitespace
    before validating, same as CardForm), capped at a generous length
    since notes accumulate as a running log inside a single TextField
    rather than one row per note (there's no separate Note model — see
    the models.py docstring / #18's own scope note about this).
    """

    text = forms.CharField(max_length=2000, widget=forms.Textarea(attrs={"rows": 2}))


class MeetingUploadForm(forms.Form):
    """Validates a #19 meeting upload: exactly one of an audio/video/
    transcript file (``file``) or pasted transcript text (``pasted_text``).

    File type is validated by extension only — content-sniffing is out of
    scope for the MVP (flagged in the issue's own implementation guidance).
    The 500 MB cap is checked here against ``UploadedFile.size`` explicitly,
    since Django's own ``FILE_UPLOAD_MAX_MEMORY_SIZE`` setting only controls
    the in-memory/temp-file threshold, not a hard reject.

    ``clean()`` also determines and stashes ``cleaned_data["kind"]`` — one of
    ``MeetingRecord.Kind`` — so the view never has to re-derive it from the
    raw upload.
    """

    AUDIO_EXTENSIONS = {"mp3", "wav", "m4a"}
    VIDEO_EXTENSIONS = {"mp4", "webm", "mov"}
    TRANSCRIPT_EXTENSIONS = {"txt", "vtt", "srt"}
    MAX_UPLOAD_SIZE = 500 * 1024 * 1024  # 500 MB, per #19's decision.

    file = forms.FileField(required=False)
    pasted_text = forms.CharField(
        required=False, widget=forms.Textarea(attrs={"rows": 10})
    )

    def clean(self):
        cleaned_data = super().clean()
        uploaded_file = cleaned_data.get("file")
        pasted_text = (cleaned_data.get("pasted_text") or "").strip()

        if uploaded_file and pasted_text:
            raise forms.ValidationError(
                "Submit either a file or pasted text, not both."
            )
        if not uploaded_file and not pasted_text:
            raise forms.ValidationError(
                "Submit an audio file, video file, transcript file, or "
                "pasted transcript text."
            )

        if uploaded_file:
            if uploaded_file.size > self.MAX_UPLOAD_SIZE:
                raise forms.ValidationError(
                    "That file is too large — uploads are capped at 500 MB."
                )
            extension = (
                uploaded_file.name.rsplit(".", 1)[-1].lower()
                if "." in uploaded_file.name
                else ""
            )
            if extension in self.AUDIO_EXTENSIONS:
                cleaned_data["kind"] = MeetingRecord.Kind.AUDIO
            elif extension in self.VIDEO_EXTENSIONS:
                cleaned_data["kind"] = MeetingRecord.Kind.VIDEO
            elif extension in self.TRANSCRIPT_EXTENSIONS:
                cleaned_data["kind"] = MeetingRecord.Kind.TRANSCRIPT_FILE
            else:
                raise forms.ValidationError(
                    "Unsupported file type. Accepted: mp3/wav/m4a (audio), "
                    "mp4/webm/mov (video), txt/vtt/srt (transcript file)."
                )
        else:
            cleaned_data["kind"] = MeetingRecord.Kind.PASTED_TEXT
            cleaned_data["pasted_text"] = pasted_text

        return cleaned_data
