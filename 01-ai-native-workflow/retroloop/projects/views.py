from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import JoinProjectForm, ProjectForm
from .models import Membership, Project


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

    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "memberships": memberships,
            "is_facilitator": is_facilitator,
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
