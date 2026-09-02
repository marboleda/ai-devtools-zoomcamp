from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProjectForm
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
            return redirect("project_created", pk=project.pk)
    else:
        form = ProjectForm()

    return render(request, "projects/create.html", {"form": form})


@login_required
def project_created(request, pk):
    project = get_object_or_404(Project, pk=pk)
    return render(request, "projects/created.html", {"project": project})
