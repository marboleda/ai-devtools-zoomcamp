from django import forms

from .models import Card, Project


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
