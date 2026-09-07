import os
import tempfile

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from django_q.tasks import async_task

from .clustering import suggest_clusters_for_cycle
from .decorators import facilitator_required
from .forms import (
    ActionItemEditForm,
    CardEditForm,
    CardForm,
    ClusterNameForm,
    DiscussionNoteForm,
    JoinProjectForm,
    ManualActionItemForm,
    ManualDecisionForm,
    MeetingUploadForm,
    ProjectForm,
    PublishSummaryForm,
    DecisionDraftEditForm,
)
from .models import (
    MAX_VOTE_WEIGHT_PER_MEMBER,
    ActionItem,
    Card,
    Cluster,
    CycleParticipation,
    DecisionDraft,
    DiscussionTopic,
    DraftSource,
    FeedbackCycle,
    MeetingRecord,
    Membership,
    Project,
    RetrospectiveSummary,
    Vote,
    generate_edit_token,
    hash_edit_token,
)

# Session key for the {card id (str): plaintext edit token} mapping kept for
# anonymous submissions in this session. Only the hash is ever persisted on
# the Card row (stack.md invariant #1); #9 reads this session entry to let
# an anonymous contributor edit their own card before the reveal.
ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY = "anonymous_card_edit_tokens"


def _anonymous_card_ids_in_session(request):
    """The card IDs (as ints) this session holds a plaintext edit token
    for, across every project/cycle the user has ever anonymously
    submitted to in this session. Callers narrow to one cycle themselves.
    """
    tokens = request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
    ids = []
    for raw_pk in tokens:
        try:
            ids.append(int(raw_pk))
        except (TypeError, ValueError):
            continue
    return ids


def _own_cards_for_cycle(request, cycle):
    # Delegates to Card.objects.visible_to (#10), the single reusable
    # visibility-scoped manager method: only request/session data is
    # touched here, and it's turned into the plain viewer/anonymous-ID
    # arguments that method takes, so the actual scoping logic lives in one
    # place instead of being re-derived per view.
    return Card.objects.visible_to(
        cycle=cycle,
        viewer=request.user,
        anonymous_card_ids=_anonymous_card_ids_in_session(request),
    )


def _assert_own_card(request, card):
    """Raise Http404 unless the current request is allowed to edit/withdraw
    ``card`` — the attributed author, or (for an anonymous card) a session
    holding the matching plaintext edit token. Mismatch and "doesn't exist"
    are both a plain Http404, so neither confirms the other case to the
    caller (same pattern as facilitator_required and create_card).
    """
    if card.author_id is not None:
        if card.author_id != request.user.id:
            raise Http404
        return

    tokens = request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {})
    token = tokens.get(str(card.pk))
    if not token or hash_edit_token(token) != card.edit_token_hash:
        raise Http404


def _get_own_card_or_404(request, project, card_id):
    card = get_object_or_404(
        Card.objects.select_related("cycle"), pk=card_id, cycle__project=project
    )
    _assert_own_card(request, card)
    return card


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
    # "N of M submitted" for #11: M is the project's member count, N is the
    # count of CycleParticipation rows for the active cycle. Never touches
    # Card, so card content/count is never exposed by this indicator.
    member_count = memberships.count()
    participation_count = (
        active_cycle.participations.count() if active_cycle is not None else None
    )
    # #23: past cycles whose summary has been published — a closed cycle
    # with no published summary (not reachable through this app's own UI,
    # since closing only ever happens via publish_summary) is excluded too,
    # so this never links to a cycle_summary page that would 404.
    published_cycles = list(
        project.cycles.filter(
            state=FeedbackCycle.State.CLOSED, summary__isnull=False
        ).order_by("-opens_at")
    )

    return render(
        request,
        "projects/detail.html",
        {
            "project": project,
            "memberships": memberships,
            "is_facilitator": is_facilitator,
            "active_cycle": active_cycle,
            "member_count": member_count,
            "participation_count": participation_count,
            "published_cycles": published_cycles,
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


@facilitator_required
@require_POST
def reveal_cycle(request, pk, project, membership):
    # Same "active cycle" lookup used by create_card: at most one non-closed
    # cycle can exist per project (DB-enforced), so there is never an
    # ambiguity about which cycle a bare "reveal" action targets.
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()

    # #12's own state check: whether the reveal is allowed is read from
    # FeedbackCycle.state, never inferred from revealed_at (which this
    # action itself is about to set). A double-click, a stale page, or an
    # attempt to reveal a cycle that hasn't started collecting yet all fail
    # here rather than mutating state a second time.
    if cycle is None or cycle.state != FeedbackCycle.State.COLLECTING:
        messages.error(
            request, "This cycle is not currently collecting submissions."
        )
        return redirect("project_detail", pk=project.pk)

    cycle.state = FeedbackCycle.State.REVEALED
    cycle.revealed_at = timezone.now()
    cycle.save(update_fields=["state", "revealed_at"])

    # #14: run once, immediately after reveal succeeds. Synchronous (no
    # Django-Q2) — that's reserved for the later meeting-transcription
    # pipeline (#20), not this call. A failed/timed-out API call is
    # swallowed inside suggest_clusters_for_cycle itself, so the cycle
    # stays "revealed" either way.
    suggest_clusters_for_cycle(cycle)

    messages.success(request, "Cards revealed.")
    return redirect("project_detail", pk=project.pk)


@login_required
def board_reveal(request, pk):
    # Same single-query, indistinguishable-404 membership lookup as
    # project_detail: any member (not just the facilitator) can view the
    # board, but a non-member — or a nonexistent project — gets a plain 404
    # per #5/#6's decision.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    # Same "active cycle" convention used throughout (create_card,
    # reveal_cycle): at most one non-closed cycle per project. Browsing a
    # past, closed cycle's board is out of scope for now — project_detail's
    # own "Previous retrospectives" section is still a placeholder.
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()

    # "Revealed or later" is read from revealed_at, not state, so this
    # covers revealed/clustering/voting/discussing alike. #14-#18's
    # clustering/voting/discussion UI is out of scope: every card is always
    # grouped by category only, never by cluster, regardless of how far the
    # cycle has actually progressed.
    revealed = cycle is not None and cycle.revealed_at is not None
    category_groups = None
    if revealed:
        # Post-reveal, visible_to returns every card in the cycle for any
        # viewer (#10) — the viewer/anonymous-ids arguments only matter
        # pre-reveal, which this branch never reaches.
        cards = list(Card.objects.visible_to(cycle=cycle, viewer=request.user))
        category_groups = [
            (value, label, [card for card in cards if card.category == value])
            for value, label in Card.Category.choices
        ]

    context = {
        "project": project,
        "cycle": cycle,
        "revealed": revealed,
        "category_groups": category_groups,
    }

    # htmx polls this same URL every ~3s (stack.md; the shared mechanism
    # #14-#18's later board modes reuse). A poll request only needs the
    # refreshed board content, not the surrounding page chrome/htmx script
    # tag, so an HX-Request carrying header gets just the fragment.
    template = (
        "projects/_board_reveal_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "projects/board_reveal.html"
    )
    return render(request, template, context)


# -- #15: manual clustering (move / merge / split / rename) --
#
# Any member — not only the facilitator (per #15's decision, since plan.md's
# clustering step describes "the team" doing this) — can act on clusters
# once the cycle has been revealed. Every mutation below shares the same
# membership lookup used elsewhere (a non-member and a nonexistent project
# are both a plain 404) and the same "active, revealed-or-later cycle"
# lookup, and returns the refreshed board_cluster fragment so the caller
# (a plain form post or the SortableJS drag handler, both via htmx) can
# swap it straight into #board-cluster — the same shared poll target #13
# already refreshes every ~3s, so a change one member makes becomes visible
# to every other member within one poll interval, not just to the person
# who made it.


def _get_active_revealed_cycle_or_404(project):
    """The project's active (non-closed) cycle, required to already be
    revealed. Clustering only ever acts on cards/clusters that are visible
    to every member, which per stack.md invariant #3 only holds from reveal
    onward. Mirrors create_card's "no reachable active cycle" 404 — these
    mutation endpoints are only ever linked to from an already-rendered,
    post-reveal board, so there is nothing to redirect back to.
    """
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None or cycle.revealed_at is None:
        raise Http404
    return cycle


def _board_cluster_context(project, cycle, viewer):
    clusters = list(
        cycle.clusters.order_by("position", "id").prefetch_related(
            Prefetch("cards", queryset=Card.objects.order_by("created_at"))
        )
    )
    # Post-reveal, visible_to returns every card in the cycle for any
    # viewer (#10) — same as board_reveal, just filtered down to the cards
    # with no cluster assigned.
    unclustered_cards = list(
        Card.objects.visible_to(cycle=cycle, viewer=viewer)
        .filter(cluster__isnull=True)
        .order_by("created_at")
    )
    return {
        "project": project,
        "cycle": cycle,
        "revealed": True,
        "clusters": clusters,
        "unclustered_cards": unclustered_cards,
    }


def _render_cluster_fragment(request, project, cycle):
    context = _board_cluster_context(project, cycle, request.user)
    return render(request, "projects/_board_cluster_fragment.html", context)


def _parse_cluster_pk(raw_value):
    """Parse a POST-supplied cluster id into an int, or ``None`` for a
    blank/absent value (used for "unclustered"). A non-numeric value (a
    malformed or tampered request — never produced by this app's own
    templates/JS) is treated as Http404 rather than crashing the request
    with an unhandled ValueError from the ORM.
    """
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None
    try:
        return int(raw_value)
    except ValueError:
        raise Http404


@login_required
def board_cluster(request, pk):
    # Same single-query, indistinguishable-404 membership lookup as
    # board_reveal: any member can view the board.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    revealed = cycle is not None and cycle.revealed_at is not None

    if revealed:
        context = _board_cluster_context(project, cycle, request.user)
    else:
        context = {
            "project": project,
            "cycle": cycle,
            "revealed": False,
            "clusters": None,
            "unclustered_cards": None,
        }

    # Same htmx poll-fragment convention as board_reveal: an HX-Request gets
    # just the refreshed board content, not the surrounding page chrome.
    template = (
        "projects/_board_cluster_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "projects/board_cluster.html"
    )
    return render(request, template, context)


@login_required
@require_POST
def move_card(request, pk, card_id):
    """Move a single card to a different cluster, or to unclustered
    (``cluster_id`` blank/absent). This is the action SortableJS's onEnd
    handler calls on every drag. Never touches cluster origin — that only
    changes on merge/split.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_revealed_cycle_or_404(project)
    card = get_object_or_404(Card, pk=card_id, cycle=cycle)

    cluster_pk = _parse_cluster_pk(request.POST.get("cluster_id"))
    if cluster_pk is not None:
        card.cluster = get_object_or_404(Cluster, pk=cluster_pk, cycle=cycle)
    else:
        card.cluster = None
    card.save(update_fields=["cluster"])

    return _render_cluster_fragment(request, project, cycle)


@login_required
@require_POST
def rename_cluster(request, pk, cluster_id):
    """Rename any cluster, regardless of origin (#15) — suggested and
    human clusters are renamed through the exact same path.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_revealed_cycle_or_404(project)
    cluster = get_object_or_404(Cluster, pk=cluster_id, cycle=cycle)

    form = ClusterNameForm(request.POST, instance=cluster)
    if form.is_valid():
        form.save()

    return _render_cluster_fragment(request, project, cycle)


@login_required
@require_POST
def merge_clusters(request, pk, cluster_id):
    """Merge the cluster named in the URL (the source) into
    ``target_cluster_id`` (the drop target), per #15's decision: the merged
    cluster keeps the target's name, its origin becomes human (it's now a
    human-driven grouping, even if the target started out suggested), and
    the source cluster's cards all move over. The now-empty source cluster
    is removed rather than left behind as an empty husk.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_revealed_cycle_or_404(project)
    source = get_object_or_404(Cluster, pk=cluster_id, cycle=cycle)

    target_pk = _parse_cluster_pk(request.POST.get("target_cluster_id"))
    target = get_object_or_404(Cluster, pk=target_pk, cycle=cycle) if target_pk is not None else None

    if target is not None and target.pk != source.pk:
        Card.objects.filter(cluster=source).update(cluster=target)
        if target.origin != Cluster.Origin.HUMAN:
            target.origin = Cluster.Origin.HUMAN
            target.save(update_fields=["origin"])
        source.delete()

    return _render_cluster_fragment(request, project, cycle)


@login_required
@require_POST
def split_cluster(request, pk, cluster_id):
    """Split a subset of the cluster's cards (``card_ids``, one or more
    POST values) out into a brand new cluster, leaving the rest of the
    cards in the original. Per #15's decision, the new cluster is
    ``origin=human`` — it's a human-driven grouping now — regardless of
    the source cluster's own origin, which this view never changes.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_revealed_cycle_or_404(project)
    source = get_object_or_404(Cluster, pk=cluster_id, cycle=cycle)

    # Only cards that actually belong to this cluster right now can be
    # split out — an ID for a card elsewhere (a different cluster,
    # unclustered, or another cycle entirely) is silently ignored rather
    # than pulled in from wherever it happens to be. A non-numeric value
    # (malformed/tampered request) is dropped the same way instead of
    # crashing the request with an unhandled ValueError from the ORM.
    card_ids = [
        int(raw_value) for raw_value in request.POST.getlist("card_ids")
        if raw_value.strip().isdigit()
    ]
    cards_to_move = Card.objects.filter(pk__in=card_ids, cluster=source)

    if cards_to_move.exists():
        name = (request.POST.get("name") or "").strip() or f"{source.name} (split)"
        new_cluster = Cluster.objects.create(
            cycle=cycle,
            name=name[:200],
            origin=Cluster.Origin.HUMAN,
            position=cycle.clusters.count(),
        )
        cards_to_move.update(cluster=new_cluster)

    return _render_cluster_fragment(request, project, cycle)


# -- #16: voting on clusters --
#
# Any member (not just the facilitator, same reasoning as #15's clustering
# actions) can freely add to or retract from their own vote allocation on
# any cluster, any number of times, at any point after reveal and before
# ``cycle.voting_closed_at`` is set (#17 sets that later; out of scope
# here). Three stackable votes per member per cycle — see
# MAX_VOTE_WEIGHT_PER_MEMBER and Vote in projects.models. No view here (or
# anywhere else in the codebase) reads Vote.objects.totals_for_cycle before
# voting closes — that manager method itself refuses to answer pre-close,
# per stack.md invariant #3, mirroring #10's Card.objects.visible_to.


def _board_vote_context(project, cycle, viewer, *, is_facilitator):
    clusters = list(cycle.clusters.order_by("position", "id"))
    # Only ever this viewer's own allocation — never an aggregate across
    # members, which Vote.objects.totals_for_cycle refuses to compute
    # before voting closes anyway (see that method's docstring).
    own_votes = {
        vote.cluster_id: vote.weight
        for vote in Vote.objects.for_member_in_cycle(cycle=cycle, member=viewer)
    }
    voting_closed = cycle.voting_closed_at is not None
    # #17: once voting has closed, Vote.objects.totals_for_cycle no longer
    # raises — every member (not just the facilitator) can now see each
    # cluster's total here, the same board #16 kept totals off of while
    # voting was open. Left as {} while voting is still open so this view
    # never calls totals_for_cycle before close, matching #16's own
    # behaviour exactly.
    totals = Vote.objects.totals_for_cycle(cycle=cycle) if voting_closed else {}
    # (cluster, this viewer's own weight on it, the cluster's total once
    # voting is closed) triples — built here rather than left as separate
    # dicts for the template to key into, the same way board_reveal's
    # category_groups pre-pairs each category with its cards.
    clusters_with_own_votes = [
        (cluster, own_votes.get(cluster.pk, 0), totals.get(cluster.pk, 0))
        for cluster in clusters
    ]
    votes_used = sum(own_votes.values())
    return {
        "project": project,
        "cycle": cycle,
        "revealed": True,
        "clusters_with_own_votes": clusters_with_own_votes,
        "votes_used": votes_used,
        "votes_remaining": MAX_VOTE_WEIGHT_PER_MEMBER - votes_used,
        "max_vote_weight": MAX_VOTE_WEIGHT_PER_MEMBER,
        "voting_closed": voting_closed,
        "is_facilitator": is_facilitator,
    }


def _render_vote_fragment(request, project, cycle, *, is_facilitator):
    context = _board_vote_context(
        project, cycle, request.user, is_facilitator=is_facilitator
    )
    return render(request, "projects/_board_vote_fragment.html", context)


@login_required
def board_vote(request, pk):
    # Same single-query, indistinguishable-404 membership lookup as
    # board_reveal/board_cluster: any member can view the board.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    revealed = cycle is not None and cycle.revealed_at is not None
    is_facilitator = membership.role == Membership.Role.FACILITATOR

    if revealed:
        context = _board_vote_context(
            project, cycle, request.user, is_facilitator=is_facilitator
        )
    else:
        context = {
            "project": project,
            "cycle": cycle,
            "revealed": False,
            "clusters_with_own_votes": None,
            "votes_used": None,
            "votes_remaining": None,
            "max_vote_weight": MAX_VOTE_WEIGHT_PER_MEMBER,
            "voting_closed": None,
            "is_facilitator": is_facilitator,
        }

    # Same htmx poll-fragment convention as board_reveal/board_cluster: an
    # HX-Request gets just the refreshed board content, not the
    # surrounding page chrome.
    template = (
        "projects/_board_vote_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "projects/board_vote.html"
    )
    return render(request, template, context)


@login_required
@require_POST
def cast_vote(request, pk, cluster_id):
    """Add (``delta=1``) or retract (``delta=-1``) one vote on a single
    cluster for the current member, in the project's active cycle. This is
    the one endpoint for every reallocation: adding to a cluster's pile,
    retracting from it, or moving a vote between clusters (retract on one,
    add on the other) — voting is an ongoing allocation, not a one-shot
    submit (#16's own decision), so it's designed to be called repeatedly.

    The 3-vote cap is enforced here, server side, regardless of what the
    client UI would have allowed: ``delta`` itself can only ever add or
    remove exactly one vote per request (any other value is a 404, the
    same "not reachable through this app's own UI" treatment
    split_cluster/move_card give a malformed request), so a crafted
    request cannot claim a bigger jump in one call. Cross-request races —
    two near-simultaneous "add a vote" calls from the same member, e.g. a
    doubled click or a scripted flood — are closed by locking the cycle
    row (``select_for_update``) inside a transaction before reading this
    member's current total, so the two calls serialize instead of both
    reading the same pre-update total and together exceeding 3. Mirrors
    Project.save's use of transaction.atomic for the same class of
    check-then-write race.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_revealed_cycle_or_404(project)
    if cycle.voting_closed_at is not None:
        # Not reachable through this app's own UI once voting has closed —
        # same treatment move_card gives a pre-reveal attempt.
        raise Http404
    cluster = get_object_or_404(Cluster, pk=cluster_id, cycle=cycle)

    raw_delta = request.POST.get("delta")
    if raw_delta not in ("1", "-1"):
        raise Http404
    delta = int(raw_delta)

    with transaction.atomic():
        # Locking the cycle row serializes every vote-cast request against
        # this cycle, so two near-simultaneous requests from the same
        # member can't both read the same pre-update total — including the
        # very first vote a member casts, before any Vote row of theirs
        # exists yet to lock instead.
        FeedbackCycle.objects.select_for_update().get(pk=cycle.pk)

        existing_votes = list(Vote.objects.filter(cycle=cycle, member=request.user))
        current_total = sum(vote.weight for vote in existing_votes)
        vote_here = next(
            (vote for vote in existing_votes if vote.cluster_id == cluster.pk), None
        )

        if delta > 0:
            if current_total >= MAX_VOTE_WEIGHT_PER_MEMBER:
                messages.error(
                    request,
                    f"You've already used all {MAX_VOTE_WEIGHT_PER_MEMBER} of your votes.",
                )
            elif vote_here is not None:
                vote_here.weight += 1
                vote_here.save(update_fields=["weight"])
            else:
                Vote.objects.create(
                    cycle=cycle, member=request.user, cluster=cluster, weight=1
                )
        elif vote_here is not None:
            if vote_here.weight <= 1:
                vote_here.delete()
            else:
                vote_here.weight -= 1
                vote_here.save(update_fields=["weight"])
        # else: retracting from a cluster with no vote on it is a no-op.

    is_facilitator = membership.role == Membership.Role.FACILITATOR
    return _render_vote_fragment(request, project, cycle, is_facilitator=is_facilitator)


# -- #17: closing voting and ranking the discussion agenda --


@facilitator_required
@require_POST
def close_voting(request, pk, project, membership):
    """One-shot facilitator action (per #6) that closes voting and produces
    the prioritized discussion agenda. Mirrors #12's reveal_cycle: a
    single-action, facilitator-only transition, rejected if already done —
    here the guard is read from ``voting_closed_at`` itself, the same field
    the action sets, rather than from ``state`` (no view in this codebase
    advances ``state`` past "revealed" yet).

    Setting ``voting_closed_at`` is what makes
    ``Vote.objects.totals_for_cycle`` (#16) stop raising ``VotingStillOpen``
    — this view calls that method immediately afterwards to compute the
    ranking, making it the first caller anywhere to see an aggregate vote
    total for this cycle.

    A ``DiscussionTopic`` row is created for every cluster in the cycle,
    including a cluster with zero votes — per #17's decision, nothing is
    dropped from the agenda; a zero-vote cluster simply sorts last. Ranked
    by vote total descending, ties broken by cluster creation order
    (``Cluster.pk`` ascending — plan.md specifies vote-based ranking but not
    a tiebreak, so #17 picks the deterministic one).
    """
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()

    if cycle is None or cycle.revealed_at is None:
        messages.error(request, "This cycle hasn't been revealed yet.")
        return redirect("project_detail", pk=project.pk)

    if cycle.voting_closed_at is not None:
        messages.error(request, "Voting is already closed for this cycle.")
        return redirect("project_detail", pk=project.pk)

    cycle.voting_closed_at = timezone.now()
    cycle.save(update_fields=["voting_closed_at"])

    totals = Vote.objects.totals_for_cycle(cycle=cycle)
    # Cluster creation order = Cluster.pk order — Cluster carries no other
    # ordering field that predates this, per #17's own guidance.
    clusters_in_creation_order = list(cycle.clusters.order_by("pk"))
    # sorted() is stable: ordering only by -total (descending) leaves
    # clusters with equal totals in their original pk-ascending order,
    # which is exactly the documented tiebreak.
    ranked_clusters = sorted(
        clusters_in_creation_order, key=lambda cluster: -totals.get(cluster.pk, 0)
    )
    DiscussionTopic.objects.bulk_create(
        DiscussionTopic(cluster=cluster, rank=rank)
        for rank, cluster in enumerate(ranked_clusters, start=1)
    )

    messages.success(request, "Voting closed. Discussion agenda is ready.")
    return redirect("project_detail", pk=project.pk)


# -- #18: discussion mode --
#
# The board's fourth and last mode, reachable once #17's close_voting has
# produced the DiscussionTopic agenda. Two different permission levels
# apply to the same page, per #18's decision: only the facilitator (#6's
# check) can set a topic's outcome, but *any* member can attach a note to
# whichever topic is currently under discussion —
# DiscussionTopic.objects.current_for_cycle (the lowest-ranked topic whose
# outcome is still blank). No AI call anywhere in this file: a note is
# stored as the plain text a member typed, nothing more.


def _board_discuss_context(project, cycle, *, is_facilitator):
    topics = list(
        DiscussionTopic.objects.for_cycle(cycle=cycle).select_related("cluster")
    )
    # Recomputed fresh on every render rather than read from a stored
    # pointer — there is no such field on any model (stack.md), by #18's
    # own decision. Once every topic has an outcome, this is None and the
    # template shows no "currently under discussion" section at all.
    current_topic = DiscussionTopic.objects.current_for_cycle(cycle=cycle)
    return {
        "project": project,
        "cycle": cycle,
        "revealed": True,
        "voting_closed": True,
        "topics": topics,
        "current_topic": current_topic,
        "is_facilitator": is_facilitator,
    }


def _render_discuss_fragment(request, project, cycle, *, is_facilitator):
    context = _board_discuss_context(project, cycle, is_facilitator=is_facilitator)
    return render(request, "projects/_board_discuss_fragment.html", context)


def _get_active_discussable_cycle_or_404(project):
    """The project's active (non-closed) cycle, required to already have
    had voting closed (#17) — that's what produces the DiscussionTopic
    agenda #18's mutation endpoints (set_topic_outcome,
    add_discussion_note) act on. Builds on
    _get_active_revealed_cycle_or_404 (#15) the same way cast_vote's own
    extra ``voting_closed_at`` check does, just inverted: those endpoints
    are only reachable *before* close, this one only *after*. Mirrors that
    helper's reasoning too — these mutation endpoints are only ever linked
    to from an already-rendered, voting-closed discuss board, so there is
    nothing to redirect back to.
    """
    cycle = _get_active_revealed_cycle_or_404(project)
    if cycle.voting_closed_at is None:
        raise Http404
    return cycle


@login_required
def board_discuss(request, pk):
    # Same single-query, indistinguishable-404 membership lookup as
    # board_reveal/board_cluster/board_vote: any member can view the board.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    revealed = cycle is not None and cycle.revealed_at is not None
    voting_closed = cycle is not None and cycle.voting_closed_at is not None
    is_facilitator = membership.role == Membership.Role.FACILITATOR

    # Discuss mode only makes sense once #17's close_voting has produced the
    # DiscussionTopic agenda — no topics exist before that. Mirrors
    # board_vote's own "not ready yet" handling: a friendly in-page message
    # rather than a 404, since any member is always allowed to *view* the
    # board, whatever stage the cycle happens to be in.
    if voting_closed:
        context = _board_discuss_context(project, cycle, is_facilitator=is_facilitator)
    else:
        context = {
            "project": project,
            "cycle": cycle,
            "revealed": revealed,
            "voting_closed": False,
            "topics": None,
            "current_topic": None,
            "is_facilitator": is_facilitator,
        }

    # Same htmx poll-fragment convention as the other three board modes: an
    # HX-Request gets just the refreshed board content, not the surrounding
    # page chrome.
    template = (
        "projects/_board_discuss_fragment.html"
        if request.headers.get("HX-Request") == "true"
        else "projects/board_discuss.html"
    )
    return render(request, template, context)


@facilitator_required
@require_POST
def set_topic_outcome(request, pk, topic_id, project, membership):
    """Facilitator-only (per #6's decorator): set one DiscussionTopic's
    outcome to discussed/skipped/deferred. Any topic belonging to the
    project's active cycle can be targeted, not only the one
    current_for_cycle currently reports — the facilitator is the one
    running the meeting and may legitimately want to mark a topic skipped
    or deferred out of strict rank order; #18's acceptance criteria places
    no such restriction on this action, only on who may perform it.
    """
    # Not reachable through this app's own UI before the agenda exists —
    # the discuss board never links to this action until then.
    cycle = _get_active_discussable_cycle_or_404(project)
    topic = get_object_or_404(DiscussionTopic, pk=topic_id, cluster__cycle=cycle)

    outcome = request.POST.get("outcome")
    valid_outcomes = {value for value, _label in DiscussionTopic.Outcome.choices}
    if outcome not in valid_outcomes:
        # A malformed/tampered request — same "not reachable through this
        # app's own UI" treatment cast_vote gives a bad delta value.
        raise Http404
    topic.outcome = outcome
    topic.save(update_fields=["outcome"])

    return _render_discuss_fragment(request, project, cycle, is_facilitator=True)


@login_required
@require_POST
def add_discussion_note(request, pk):
    """Any member (not only the facilitator, per #18's decision and
    plan.md's "team members can manually record notes") can attach a
    free-text note to the topic currently under discussion. There is no
    topic id in this URL at all: the target is always
    DiscussionTopic.objects.current_for_cycle, computed server-side, so a
    member can never attach a note to a topic that isn't the current one
    just by crafting a request.

    Notes accumulate rather than overwrite — DiscussionTopic.notes is a
    single TextField (stack.md), not a separate one-row-per-note model, so
    each new note is appended as its own paragraph, prefixed with the
    author's username, after whatever is already there.
    """
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = _get_active_discussable_cycle_or_404(project)
    topic = DiscussionTopic.objects.current_for_cycle(cycle=cycle)
    if topic is None:
        # Every topic already has an outcome (or there are no topics at
        # all) — nothing is "currently under discussion" to attach a note
        # to.
        raise Http404

    form = DiscussionNoteForm(request.POST)
    if form.is_valid():
        entry = f"{request.user.username}: {form.cleaned_data['text']}"
        topic.notes = f"{topic.notes}\n\n{entry}" if topic.notes else entry
        topic.save(update_fields=["notes"])

    is_facilitator = membership.role == Membership.Role.FACILITATOR
    return _render_discuss_fragment(request, project, cycle, is_facilitator=is_facilitator)


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

            # #11: participation is tracked per member (request.user), not
            # per card and not per Card.author — an anonymous submission
            # still records that this member submitted, without linking
            # them to the card. get_or_create relies on the unique
            # (cycle, member) constraint so a second card from the same
            # member in the same cycle never creates a second row.
            CycleParticipation.objects.get_or_create(cycle=cycle, member=request.user)

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

    # Only while collecting (#9): once the cycle moves on, cards can no
    # longer be edited/withdrawn anyway, and pre-reveal visibility only
    # ever needs to include what you're allowed to act on right now.
    own_cards = _own_cards_for_cycle(request, cycle) if collecting else None

    return render(
        request,
        "projects/card_form.html",
        {
            "project": project,
            "cycle": cycle,
            "collecting": collecting,
            "form": form,
            "own_cards": own_cards,
        },
    )


@login_required
def edit_card(request, pk, card_id):
    # Same single-query, indistinguishable-404 membership lookup used by
    # create_card / project_detail.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    card = _get_own_card_or_404(request, project, card_id)

    if card.cycle.state != FeedbackCycle.State.COLLECTING:
        # Rejected for every card in the cycle once it's left "collecting",
        # even the owner's — #9's own rule, which #12's reveal depends on.
        # This is a normal, expected rejection (the owner already knows
        # their own card and the cycle's state), not a privacy leak, so it
        # gets a message + redirect rather than a 404.
        messages.error(request, "This cycle is no longer collecting submissions.")
        return redirect("create_card", pk=project.pk)

    if request.method == "POST":
        form = CardEditForm(request.POST)
        if form.is_valid():
            # Only category/text are touched — author and edit_token_hash
            # are never assigned here, so attribution cannot change as a
            # side effect of this view (see CardEditForm too).
            card.category = form.cleaned_data["category"]
            card.text = form.cleaned_data["text"]
            card.save(update_fields=["category", "text"])
            messages.success(request, "Card updated.")
            return redirect("create_card", pk=project.pk)
    else:
        form = CardEditForm(initial={"category": card.category, "text": card.text})

    return render(
        request,
        "projects/card_edit.html",
        {"project": project, "cycle": card.cycle, "card": card, "form": form},
    )


@login_required
@require_POST
def withdraw_card(request, pk, card_id):
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    card = _get_own_card_or_404(request, project, card_id)

    if card.cycle.state != FeedbackCycle.State.COLLECTING:
        messages.error(request, "This cycle is no longer collecting submissions.")
        return redirect("create_card", pk=project.pk)

    was_anonymous = card.author_id is None
    card.delete()

    if was_anonymous:
        # Drop the now-dangling token entry so a stale token for a deleted
        # card doesn't linger in the session.
        tokens = dict(request.session.get(ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {}))
        if tokens.pop(str(card_id), None) is not None:
            request.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY] = tokens

    messages.success(request, "Card withdrawn.")
    return redirect("create_card", pk=project.pk)


# -- #19: meeting upload page --
#
# Facilitator-only (per #6's decorator), a full page rather than one of the
# retrospective board's own htmx-fragment modes (#13/#15/#16/#18) — the
# board modes all render the same collaborative "board" for every member;
# this is a distinct, one-person action. It still reuses the board modes'
# ~3s htmx-polling pattern, just scoped to a small status fragment
# (_meeting_upload_status_fragment.html) rather than the whole page, since
# the form itself doesn't need to be re-rendered every poll.
#
# Retroactively adjusted by #20 (see the comment on issue #20 explaining
# why): a transcript_file upload is now handled exactly like pasted_text —
# read directly, straight to a completed MeetingRecord, no transcription
# background job — rather than #19's original PENDING + async_task
# behavior. Only audio/video still enqueue
# projects.tasks.process_meeting_record, and #20 also added writing that
# upload to a local temp file before enqueuing, since #19 never persisted
# it anywhere and process_meeting_record runs in a separate worker process.
#
# #21 adds a second kind of background job, projects.tasks.
# extract_decisions_and_actions, enqueued from all three of this view's
# completion paths (pasted_text and transcript_file here, plus
# process_meeting_record's own success path for audio/video) — see that
# module's docstring. It runs independently of the "actively processing"
# state tracked below, which only ever concerns transcription: a
# pasted_text/transcript_file record is COMPLETED the moment it's created
# and stays that way regardless of how its own extraction job turns out.


def _meeting_upload_status_context(project, cycle):
    records = list(cycle.meeting_records.all())
    return {"project": project, "cycle": cycle, "records": records}


@facilitator_required
def meeting_upload(request, pk, project, membership):
    # Same "active (non-closed) cycle" convention used throughout
    # (create_card, reveal_cycle, board_reveal): at most one non-closed
    # cycle can exist per project. There's nothing to upload a meeting
    # record against without one.
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None:
        raise Http404

    form = MeetingUploadForm()

    if request.method == "POST":
        form = MeetingUploadForm(request.POST, request.FILES)
        if form.is_valid():
            kind = form.cleaned_data["kind"]

            if kind == MeetingRecord.Kind.PASTED_TEXT:
                # No file, no transcription background job: transcript_text
                # is populated directly from the form and processing_state
                # goes straight to completed — there is nothing left to
                # transcribe (#19's explicit decision), so this can never be
                # "actively processing" and never blocks a later upload.
                record = MeetingRecord.objects.create(
                    cycle=cycle,
                    kind=kind,
                    transcript_text=form.cleaned_data["pasted_text"],
                    processing_state=MeetingRecord.ProcessingState.COMPLETED,
                )
                # #21: extraction runs automatically as soon as the
                # transcript is ready, never on a separate facilitator
                # click. Enqueued as a Django-Q2 task rather than called
                # inline here, same reasoning as the audio/video branch
                # below: an AI call never runs synchronously inside a
                # request/response cycle in this codebase.
                async_task("projects.tasks.extract_decisions_and_actions", record.pk)
                messages.success(request, "Transcript saved.")
                return redirect("meeting_upload", pk=project.pk)

            elif kind == MeetingRecord.Kind.TRANSCRIPT_FILE:
                # Retroactive fix for the #19/#20 contradiction flagged on
                # #20: a .txt/.vtt/.srt upload's content already *is* the
                # transcript, so this mirrors the pasted_text branch above
                # exactly — read it directly, no transcription background
                # job, straight to completed, and (since it can never be
                # non-terminal) no "actively processing" check either.
                uploaded_file = form.cleaned_data["file"]
                try:
                    transcript_text = uploaded_file.read().decode("utf-8")
                except UnicodeDecodeError:
                    form.add_error(
                        None,
                        "That transcript file isn't valid UTF-8 text. "
                        "Please upload a plain-text transcript file.",
                    )
                else:
                    record = MeetingRecord.objects.create(
                        cycle=cycle,
                        kind=kind,
                        transcript_text=transcript_text,
                        processing_state=MeetingRecord.ProcessingState.COMPLETED,
                    )
                    # #21: same reasoning as the pasted_text branch above.
                    async_task(
                        "projects.tasks.extract_decisions_and_actions", record.pk
                    )
                    messages.success(request, "Transcript saved.")
                    return redirect("meeting_upload", pk=project.pk)

            # Only audio/video reach here. Only one MeetingRecord can be
            # actively processing per cycle at a time (#19's decision) — a
            # second upload while one is still pending/processing is
            # rejected with a clear message, not queued behind it. Checked
            # here, inside the same request that would otherwise create the
            # second row, rather than left to the queue to sort out later.
            elif MeetingRecord.objects.active_processing_for_cycle(cycle=cycle).exists():
                form.add_error(
                    None,
                    "A meeting record is already being processed for this "
                    "cycle. Wait for it to finish before uploading another.",
                )
            else:
                uploaded_file = form.cleaned_data["file"]
                record = MeetingRecord.objects.create(
                    cycle=cycle,
                    kind=kind,
                    processing_state=MeetingRecord.ProcessingState.PENDING,
                )
                # #20: process_meeting_record runs in a separate worker
                # process, potentially well after this request has finished
                # — #19 never persisted the upload anywhere, so the
                # request-scoped upload is gone by the time a worker would
                # pick the job up. Written to a local temp file (never to
                # the model, never permanently — stack.md's no-persistent-
                # media rule) that both `web` and `worker` can reach via the
                # shared volume stack.md's deployment section describes; for
                # this codebase/tests that's just tempfile.gettempdir().
                suffix = os.path.splitext(uploaded_file.name)[1]
                fd, temp_path = tempfile.mkstemp(
                    suffix=suffix, dir=tempfile.gettempdir()
                )
                with os.fdopen(fd, "wb") as temp_file:
                    for chunk in uploaded_file.chunks():
                        temp_file.write(chunk)

                # django-q2 doesn't validate the dotted path at enqueue
                # time, only when a worker picks the job up.
                task_id = async_task(
                    "projects.tasks.process_meeting_record", record.pk, temp_path
                )
                record.task_id = task_id or ""
                record.save(update_fields=["task_id"])
                messages.success(
                    request, "Upload received. Processing has started."
                )
                return redirect("meeting_upload", pk=project.pk)

    context = _meeting_upload_status_context(project, cycle)
    context["form"] = form
    return render(request, "projects/meeting_upload.html", context)


@facilitator_required
def meeting_upload_status(request, pk, project, membership):
    """The ~3s htmx poll target for meeting_upload.html's status section
    (stack.md's polling pattern, reused per #19's own guidance) — returns
    just the status fragment, never the surrounding page chrome or the
    upload form.
    """
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None:
        raise Http404

    context = _meeting_upload_status_context(project, cycle)
    return render(request, "projects/_meeting_upload_status_fragment.html", context)


# -- #22: facilitator review and confirmation of drafts --
#
# Facilitator-only (per #6's decorator), a full page like meeting_upload
# rather than one of the retrospective board's own htmx-fragment modes: this
# is a distinct, one-person action, not something every member watches
# update live. Lists every DecisionDraft/ActionItem for the project's
# active cycle with confirmed_at still NULL (per #21's extraction, that's
# every AI-sourced row until a facilitator acts on it here). Each listed
# row carries its own edit-then-confirm form (submitting "Confirm" saves
# whatever the facilitator changed and sets confirmed_at/confirmed_by in
# the same request) and its own one-click "Discard" form (a hard delete,
# per #22's own decision — stack.md defines no "discarded" state). A
# separate pair of forms at the bottom lets the facilitator add a brand new
# decision or action item that didn't come from AI extraction at all;
# those are saved with source=manual and confirmed immediately, since
# there's no draft stage for something the facilitator is typing
# themselves right now.


def _active_cycle_or_404(project):
    """The project's active (non-closed) cycle. Mirrors the "no reachable
    active cycle" 404 create_card and _get_active_revealed_cycle_or_404 use
    elsewhere: the review screen and its action endpoints are only ever
    linked to from an already-rendered page for a project that has one.
    """
    cycle = project.cycles.exclude(state=FeedbackCycle.State.CLOSED).first()
    if cycle is None:
        raise Http404
    return cycle


def _review_drafts_context(project, cycle):
    decision_drafts = list(
        DecisionDraft.objects.unconfirmed_for_cycle(cycle=cycle).order_by("pk")
    )
    action_items = list(
        ActionItem.objects.unconfirmed_for_cycle(cycle=cycle)
        .select_related("owner")
        .order_by("pk")
    )
    decision_rows = [
        (draft, DecisionDraftEditForm(instance=draft)) for draft in decision_drafts
    ]
    action_rows = [
        (item, ActionItemEditForm(instance=item, project=project))
        for item in action_items
    ]
    return {
        "project": project,
        "cycle": cycle,
        "decision_rows": decision_rows,
        "action_rows": action_rows,
        "new_decision_form": ManualDecisionForm(),
        "new_action_item_form": ManualActionItemForm(project=project),
    }


@facilitator_required
def review_drafts(request, pk, project, membership):
    cycle = _active_cycle_or_404(project)
    context = _review_drafts_context(project, cycle)
    return render(request, "projects/review_drafts.html", context)


@facilitator_required
@require_POST
def confirm_decision_draft(request, pk, draft_id, project, membership):
    """Save whatever text edit the facilitator made (if any) and confirm
    this DecisionDraft in the same request — the sole place confirmed_at/
    confirmed_by ever gets set for an AI-sourced decision (#22's own
    decision). Only reachable for a draft that's still unconfirmed and
    belongs to the project's active cycle: get_object_or_404 with those
    filters means a double-submit or a stale page can't re-confirm (and
    re-timestamp) an already-confirmed row.
    """
    cycle = _active_cycle_or_404(project)
    draft = get_object_or_404(
        DecisionDraft, pk=draft_id, cycle=cycle, confirmed_at__isnull=True
    )
    form = DecisionDraftEditForm(request.POST, instance=draft)
    if form.is_valid():
        draft = form.save(commit=False)
        draft.confirmed_at = timezone.now()
        draft.confirmed_by = request.user
        draft.save()
        messages.success(request, "Decision confirmed.")
    else:
        messages.error(request, "Couldn't confirm that decision — check the text.")
    return redirect("review_drafts", pk=project.pk)


@facilitator_required
@require_POST
def discard_decision_draft(request, pk, draft_id, project, membership):
    """Hard-delete an unconfirmed DecisionDraft — per #22's own decision,
    there is no soft-delete/"discarded" state: a rejected draft has no
    further use. Same unconfirmed-only guard as confirm_decision_draft, so
    an already-confirmed row can never be discarded through this endpoint.
    """
    cycle = _active_cycle_or_404(project)
    draft = get_object_or_404(
        DecisionDraft, pk=draft_id, cycle=cycle, confirmed_at__isnull=True
    )
    draft.delete()
    messages.success(request, "Decision discarded.")
    return redirect("review_drafts", pk=project.pk)


@facilitator_required
@require_POST
def confirm_action_item(request, pk, item_id, project, membership):
    """Same shape as confirm_decision_draft, for an ActionItem's owner/due
    date — the two fields #22 names as editable before confirming.
    """
    cycle = _active_cycle_or_404(project)
    item = get_object_or_404(
        ActionItem, pk=item_id, cycle=cycle, confirmed_at__isnull=True
    )
    form = ActionItemEditForm(request.POST, instance=item, project=project)
    if form.is_valid():
        item = form.save(commit=False)
        item.confirmed_at = timezone.now()
        item.confirmed_by = request.user
        item.save()
        messages.success(request, "Action item confirmed.")
    else:
        messages.error(request, "Couldn't confirm that action item.")
    return redirect("review_drafts", pk=project.pk)


@facilitator_required
@require_POST
def discard_action_item(request, pk, item_id, project, membership):
    """Hard-delete an unconfirmed ActionItem. Same reasoning and guard as
    discard_decision_draft above.
    """
    cycle = _active_cycle_or_404(project)
    item = get_object_or_404(
        ActionItem, pk=item_id, cycle=cycle, confirmed_at__isnull=True
    )
    item.delete()
    messages.success(request, "Action item discarded.")
    return redirect("review_drafts", pk=project.pk)


@facilitator_required
@require_POST
def create_decision_draft(request, pk, project, membership):
    """Add a brand-new decision that didn't come from AI extraction —
    source=manual, and confirmed immediately (confirmed_at/confirmed_by set
    on creation) since there's no draft stage for something the facilitator
    is typing themselves right now, per #22's own decision.
    """
    cycle = _active_cycle_or_404(project)
    form = ManualDecisionForm(request.POST)
    if form.is_valid():
        draft = form.save(commit=False)
        draft.cycle = cycle
        draft.source = DraftSource.MANUAL
        draft.confirmed_at = timezone.now()
        draft.confirmed_by = request.user
        draft.save()
        messages.success(request, "Decision added.")
    else:
        messages.error(request, "Couldn't add that decision — check the text.")
    return redirect("review_drafts", pk=project.pk)


@facilitator_required
@require_POST
def create_action_item(request, pk, project, membership):
    """Same shape as create_decision_draft, for a manually-added action
    item.
    """
    cycle = _active_cycle_or_404(project)
    form = ManualActionItemForm(request.POST, project=project)
    if form.is_valid():
        item = form.save(commit=False)
        item.cycle = cycle
        item.source = DraftSource.MANUAL
        item.confirmed_at = timezone.now()
        item.confirmed_by = request.user
        item.save()
        messages.success(request, "Action item added.")
    else:
        messages.error(request, "Couldn't add that action item — check the description.")
    return redirect("review_drafts", pk=project.pk)


# -- #23: publishing the retrospective summary --
#
# Unlike the other stage screens (board_reveal, review_drafts, ...), this
# view is addressed by a specific cycle id rather than "the project's
# active cycle" — publishing is exactly what moves a cycle to state=closed
# (see publish_summary below), so a lookup keyed on "not closed" would stop
# finding it the instant it's published. That also means a past, already
# published cycle's summary stays reachable — project_detail's "Previous
# retrospectives" section links to exactly these (closed cycles with a
# published summary), and the active cycle's own facilitator-only section
# links here too, for the not-yet-published preview.
#
# Per plan.md's role table, a plain member can "view completed retrospective
# summaries" — so unlike review_drafts/meeting_upload, this screen is not
# behind @facilitator_required. Before a summary has been published, only
# the facilitator can reach it at all (to preview the data and publish);
# a member gets a 404, the same "not there yet" treatment other pre-stage
# views use.


def _cycle_summary_context(project, cycle, viewer, *, is_facilitator, summary):
    # "Top discussion topics (by #17's rank)" and "key notes (from #18)":
    # DiscussionTopic.objects.for_cycle already orders by rank (its Meta's
    # default ordering) and carries its own notes field — nothing extra to
    # compute here.
    topics = list(
        DiscussionTopic.objects.for_cycle(cycle=cycle).select_related("cluster")
    )
    # "Confirmed decisions and confirmed action items only": the *only*
    # query path per #22's own docstring — never unconfirmed_for_cycle,
    # never the bare manager.
    decisions = list(
        DecisionDraft.objects.confirmed_for_cycle(cycle=cycle).order_by("pk")
    )
    action_items = list(
        ActionItem.objects.confirmed_for_cycle(cycle=cycle)
        .select_related("owner")
        .order_by("pk")
    )
    # "Attendance/participation": CycleParticipation is the only signal
    # available (#11) — who submitted feedback, not who "attended" in any
    # richer sense (stack.md has no separate meeting-attendance record).
    participations = list(
        cycle.participations.select_related("member").order_by("submitted_at")
    )
    member_count = project.memberships.count()
    # "The original feedback cards", anonymity preserved: Card.objects.
    # visible_to (#10) returns every card for any viewer once the cycle is
    # revealed, and an anonymous card's .author stays None straight from
    # the query — the template renders it exactly like board_reveal's
    # fragment does, no author shown.
    cards = list(Card.objects.visible_to(cycle=cycle, viewer=viewer))
    category_groups = [
        (value, label, [card for card in cards if card.category == value])
        for value, label in Card.Category.choices
    ]
    return {
        "project": project,
        "cycle": cycle,
        "is_facilitator": is_facilitator,
        "summary": summary,
        "published": summary is not None,
        "topics": topics,
        "decisions": decisions,
        "action_items": action_items,
        "participations": participations,
        "participation_count": len(participations),
        "member_count": member_count,
        "category_groups": category_groups,
    }


@login_required
def cycle_summary(request, pk, cycle_id):
    # Same single-query, indistinguishable-404 membership lookup used
    # throughout: a non-member and a nonexistent project are both a plain
    # 404.
    membership = get_object_or_404(
        Membership.objects.select_related("project"), project_id=pk, user=request.user
    )
    project = membership.project
    cycle = get_object_or_404(FeedbackCycle, pk=cycle_id, project=project)
    is_facilitator = membership.role == Membership.Role.FACILITATOR

    if cycle.revealed_at is None:
        # Nothing to summarize before reveal — not reachable through this
        # app's own UI at this stage, same treatment
        # _get_active_revealed_cycle_or_404 gives elsewhere.
        raise Http404

    summary = RetrospectiveSummary.objects.filter(cycle=cycle).first()
    if summary is None and not is_facilitator:
        # Per plan.md, a member can view *completed* retrospective
        # summaries — nothing has been published for this cycle yet, so
        # there is nothing here for a non-facilitator to see.
        raise Http404

    context = _cycle_summary_context(
        project, cycle, request.user, is_facilitator=is_facilitator, summary=summary
    )

    if summary is None:
        # Facilitator-only preview, with a publish form pre-filled from
        # #21's extraction summary when there is one — the facilitator can
        # still edit or clear it before publishing.
        default_body = ""
        latest_record = (
            cycle.meeting_records.filter(
                processing_state=MeetingRecord.ProcessingState.COMPLETED
            )
            .exclude(extracted_summary="")
            .first()
        )
        if latest_record is not None:
            default_body = latest_record.extracted_summary
        context["publish_form"] = PublishSummaryForm(initial={"body": default_body})

    return render(request, "projects/cycle_summary.html", context)


@facilitator_required
@require_POST
def publish_summary(request, pk, cycle_id, project, membership):
    """One-shot facilitator action (per #6): creates the cycle's
    ``RetrospectiveSummary`` row and, in the same request, sets
    ``FeedbackCycle.completed_at`` and moves ``state`` to ``closed`` — the
    cycle's terminal state. ``state`` (not ``completed_at``) is what #7's
    ``unique_active_cycle_per_project`` constraint is keyed on
    (``state != closed``), so this is what actually lets ``create_cycle``
    succeed again for the same project afterwards.

    Rejected without creating a second row if a summary already exists for
    this cycle — checked up front for a friendly message, and backstopped
    by ``cycle``'s OneToOneField uniqueness (an IntegrityError from a
    genuine check-then-create race is caught the same way create_cycle
    catches one on its own unique constraint).
    """
    cycle = get_object_or_404(FeedbackCycle, pk=cycle_id, project=project)

    if RetrospectiveSummary.objects.filter(cycle=cycle).exists():
        messages.error(request, "This cycle's summary has already been published.")
        return redirect("cycle_summary", pk=project.pk, cycle_id=cycle.pk)

    form = PublishSummaryForm(request.POST)
    body = form.cleaned_data["body"] if form.is_valid() else ""

    try:
        with transaction.atomic():
            RetrospectiveSummary.objects.create(cycle=cycle, body=body)
    except IntegrityError:
        messages.error(request, "This cycle's summary has already been published.")
        return redirect("cycle_summary", pk=project.pk, cycle_id=cycle.pk)

    cycle.completed_at = timezone.now()
    cycle.state = FeedbackCycle.State.CLOSED
    cycle.save(update_fields=["completed_at", "state"])

    messages.success(request, "Retrospective summary published.")
    return redirect("cycle_summary", pk=project.pk, cycle_id=cycle.pk)
