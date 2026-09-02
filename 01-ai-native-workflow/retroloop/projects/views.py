from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .decorators import facilitator_required
from .forms import CardForm, JoinProjectForm, ProjectForm
from .models import Card, FeedbackCycle, Membership, Project, generate_edit_token, hash_edit_token

# Session key for the {card id (str): plaintext edit token} mapping kept for
# anonymous submissions in this session. Only the hash is ever persisted on
# the Card row (stack.md invariant #1); #9 reads this session entry to let
# an anonymous contributor edit their own card before the reveal.
ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY = "anonymous_card_edit_tokens"


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


@login_required
def create_card(request, pk):
    # Same single-query membership lookup as project_detail /
    # facilitator_required: a nonexistent project and an existing project
    # the user isn't a member of are indistinguishable, both 404, so the
    # route never confirms a project's existence to a non-member.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None:
        # No active cycle at all: the route is unreachable.
        raise Http404

    collecting = cycle.state == FeedbackCycle.State.COLLECTING
    form = None

    if request.method == "POST" and collecting:
        form = CardForm(request.POST)
        if form.is_valid():
            card = Card(
                cycle=cycle,
                category=form.cleaned_data["category"],
                text=form.cleaned_data["text"],
            )
            if form.cleaned_data["anonymous"]:
                token = generate_edit_token()
                card.author = None
                card.edit_token_hash = hash_edit_token(token)
            else:
                card.author = request.user
            card.save()

            if form.cleaned_data["anonymous"]:
                tokens = dict(
                    request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
                )
                tokens[str(card.pk)] = token
                request.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY] = tokens

            # Back to a cleared form, not project_detail, so submitting
            # several cards in a row doesn't require re-navigating.
            form = CardForm()
    elif collecting:
        form = CardForm()

    return render(
        request,
        "projects/card_form.html",
        {
            "project": project,
            "cycle": cycle,
            "collecting": collecting,
            "form": form,
        },
    )
