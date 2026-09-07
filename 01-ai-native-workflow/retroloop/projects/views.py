from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.db.models import Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .clustering import suggest_clusters_for_cycle
from .decorators import facilitator_required
from .forms import CardEditForm, CardForm, ClusterNameForm, JoinProjectForm, ProjectForm
from .models import (
    MAX_VOTE_WEIGHT_PER_MEMBER,
    Card,
    Cluster,
    CycleParticipation,
    DiscussionTopic,
    FeedbackCycle,
    Membership,
    Project,
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
