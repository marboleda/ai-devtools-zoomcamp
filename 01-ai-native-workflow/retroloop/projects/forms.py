from django import forms

from .models import Project


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
