from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .decorators import facilitator_required
from .forms import JoinProjectForm, ProjectForm
from .models import FeedbackCycle, Membership, Project


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

    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "memberships": memberships,
            "is_facilitator": is_facilitator,
            "active_cycle": active_cycle,
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
