import hashlib
import secrets

from django.conf import settings
from django.db import IntegrityError, models, transaction

# 8 characters, uppercase letters and digits, excluding visually ambiguous
# characters (0/O, 1/I/L) — decision recorded on GitHub issue #3.
JOIN_CODE_LENGTH = 8
JOIN_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
JOIN_CODE_MAX_ATTEMPTS = 10


def generate_join_code():
    return "".join(
        secrets.choice(JOIN_CODE_ALPHABET) for _ in range(JOIN_CODE_LENGTH)
    )


class Project(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="projects_created",
    )
    join_code = models.CharField(max_length=JOIN_CODE_LENGTH, unique=True, editable=False)

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self._state.adding and not self.join_code:
            # Generated here, not entered by the user. A DB-level unique
            # constraint is the real guarantee; on the rare collision we
            # regenerate and retry rather than fail the request.
            for attempt in range(JOIN_CODE_MAX_ATTEMPTS):
                self.join_code = generate_join_code()
                try:
                    with transaction.atomic():
                        super().save(*args, **kwargs)
                    return
                except IntegrityError:
                    if attempt == JOIN_CODE_MAX_ATTEMPTS - 1:
                        raise
                    continue
        else:
            super().save(*args, **kwargs)


class Membership(models.Model):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        FACILITATOR = "facilitator", "Facilitator"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=20, choices=Role.choices)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "user"], name="unique_project_membership"
            )
        ]

    def __str__(self):
        return f"{self.user} in {self.project} ({self.role})"


class FeedbackCycle(models.Model):
    class State(models.TextChoices):
        COLLECTING = "collecting", "Collecting"
        REVEALED = "revealed", "Revealed"
        CLUSTERING = "clustering", "Clustering"
        VOTING = "voting", "Voting"
        DISCUSSING = "discussing", "Discussing"
        CLOSED = "closed", "Closed"

    project = models.ForeignKey(
        Project, on_delete=models.CASCADE, related_name="cycles"
    )
    # Auto-generated label ("Cycle N"), not entered by the facilitator — see
    # the create_cycle view.
    week = models.CharField(max_length=100)
    state = models.CharField(
        max_length=20, choices=State.choices, default=State.COLLECTING
    )
    # Set at creation time; not entered by the facilitator.
    opens_at = models.DateTimeField(auto_now_add=True)
    # Set by later stage transitions (#12 reveal, #17 close voting, #23
    # publish) — NULL until then.
    closes_at = models.DateTimeField(null=True, blank=True)
    revealed_at = models.DateTimeField(null=True, blank=True)
    voting_closed_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opens_at"]
        constraints = [
            # DB-level guarantee that a project has at most one non-closed
            # cycle at a time. This is a partial unique index (state !=
            # closed), so it holds even under a check-then-create race
            # between two near-simultaneous requests — the loser gets an
            # IntegrityError instead of a second active row ever existing.
            models.UniqueConstraint(
                fields=["project"],
                condition=~models.Q(state="closed"),
                name="unique_active_cycle_per_project",
            )
        ]

    def __str__(self):
        return f"{self.project} — {self.week} ({self.state})"


class CycleParticipation(models.Model):
    """(cycle, member, submitted_at) — created the first time ``member``
    saves a card in ``cycle``; a second card from the same member in the
    same cycle does not create a second row (see the unique constraint
    below and its use with ``get_or_create`` in projects.views.create_card).

    Deliberately holds no card data — per stack.md invariant #1, this
    answers "who still needs to submit?" and feeds the participation-rate
    indicator without ever linking a person to card content.
    """

    cycle = models.ForeignKey(
        FeedbackCycle, on_delete=models.CASCADE, related_name="participations"
    )
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="cycle_participations",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["cycle", "member"], name="unique_cycle_participation"
            )
        ]

    def __str__(self):
        return f"{self.member} participated in {self.cycle}"


class Cluster(models.Model):
    """Minimal stub per stack.md's data model sketch, added only so
    ``Card.cluster`` (below) has something to point at. No clustering
    behaviour, view, or admin registration ships with #8 — that's a later
    issue's scope; this just avoids leaving ``Card`` without the field its
    own spec names.
    """

    class Origin(models.TextChoices):
        SUGGESTED = "suggested", "Suggested"
        HUMAN = "human", "Human"

    cycle = models.ForeignKey(
        FeedbackCycle, on_delete=models.CASCADE, related_name="clusters"
    )
    name = models.CharField(max_length=200)
    origin = models.CharField(max_length=20, choices=Origin.choices)
    position = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.name


def generate_edit_token():
    return secrets.token_urlsafe(32)


def hash_edit_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


class CardQuerySet(models.QuerySet):
    def visible_to(self, *, cycle, viewer, anonymous_card_ids=()):
        """The cards from ``cycle`` that ``viewer`` is allowed to see, per
        stack.md invariant #3.

        Pre-reveal (``cycle.revealed_at`` is ``None``): only the viewer's
        own cards — those attributed to ``viewer``, plus any whose ID is in
        ``anonymous_card_ids``, the caller-supplied collection of anonymous
        cards this viewer owns in their current session. There is no
        facilitator exception; role is never consulted here.

        At and after reveal (``cycle.revealed_at`` is set — covers
        revealed/clustering/voting/discussing/closed): every card in the
        cycle.

        Deliberately takes no ``request``/session — the caller (a view) is
        responsible for turning session data into the explicit
        ``anonymous_card_ids`` collection, so this stays unit-testable
        without an HTTP request.

        Results are always scoped to the single ``cycle`` passed in; a card
        from a different cycle is never returned even for the same viewer.
        An empty/omitted ``anonymous_card_ids`` never widens the result —
        it simply contributes no extra rows, it never falls back to "all
        anonymous cards".
        """
        base = self.filter(cycle=cycle).order_by("created_at")
        if cycle.revealed_at is not None:
            return base
        return base.filter(
            models.Q(author=viewer) | models.Q(pk__in=list(anonymous_card_ids or ()))
        )


CardManager = models.Manager.from_queryset(CardQuerySet)


class Card(models.Model):
    class Category(models.TextChoices):
        START = "start", "Start"
        STOP = "stop", "Stop"
        CONTINUE = "continue", "Continue"

    objects = CardManager()

    cycle = models.ForeignKey(FeedbackCycle, on_delete=models.CASCADE, related_name="cards")
    category = models.CharField(max_length=20, choices=Category.choices)
    text = models.CharField(max_length=280)
    # NULL for an anonymous card — per stack.md invariant #1, no reference
    # back to the submitter is stored anywhere else on this row either.
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cards",
    )
    # Populated only for an anonymous card. Holds a hash only — the
    # plaintext token lives in the submitter's session (see
    # projects.views.create_card) and nowhere else, per stack.md invariant
    # #1. NULL for an attributed card.
    edit_token_hash = models.CharField(max_length=64, null=True, blank=True)
    cluster = models.ForeignKey(
        Cluster,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cards",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.get_category_display()} card in {self.cycle}"


# -- #16: voting on clusters --
#
# Three stackable votes per member per cycle, spent on clusters only (a card
# left unclustered has nothing to vote on — stack.md's Vote model has no
# card FK at all). Reallocating is ongoing, not a one-shot submit: a member
# can add to, retract from, or move between clusters at any time before
# ``cycle.voting_closed_at`` is set (#17's later job). The 3-vote cap and
# the pre-close privacy gate both live here / in projects.views.cast_vote,
# never only in a template — see VoteQuerySet.totals_for_cycle below and
# its use of stack.md invariant #3, the same pattern Card.objects.visible_to
# (#10) uses for pre-reveal cards.

MAX_VOTE_WEIGHT_PER_MEMBER = 3


class VotingStillOpen(Exception):
    """Raised by Vote.objects.totals_for_cycle when
    ``cycle.voting_closed_at`` is not yet set. There is no facilitator
    exception and no way to opt out of the gate: a caller that wants
    cluster vote totals before voting has closed gets an exception, not an
    empty/partial queryset it could mistake for "no votes yet". Closing
    voting and reading totals after that point is #17's job, not this
    one's — this exception is what #17's view will need to stop tripping.
    """


class VoteQuerySet(models.QuerySet):
    def for_member_in_cycle(self, *, cycle, member):
        """``member``'s own vote rows in ``cycle`` — never anyone else's,
        so this needs no privacy gate: a member always knows their own
        current allocation, before or after voting closes. Used to render
        "your votes" in the vote-mode board and to compute how many of the
        member's 3 votes remain unspent.
        """
        return self.filter(cycle=cycle, member=member)

    def total_weight_for_member_in_cycle(self, *, cycle, member):
        """The sum of ``weight`` across every vote ``member`` currently has
        in ``cycle`` — used to enforce the 3-vote cap in
        projects.views.cast_vote. Same no-gate reasoning as
        ``for_member_in_cycle``: this is only ever the caller's own total.
        """
        total = self.filter(cycle=cycle, member=member).aggregate(
            total=models.Sum("weight")
        )["total"]
        return total or 0

    def totals_for_cycle(self, *, cycle):
        """``{cluster_id: total_weight}`` across every member's vote in
        ``cycle`` — the number #17's discussion-agenda ranking will need.

        Gated per stack.md invariant #3, the same way
        ``Card.objects.visible_to`` gates pre-reveal cards (#10): raises
        ``VotingStillOpen`` while ``cycle.voting_closed_at`` is unset. This
        is the *only* path to an aggregate vote count anywhere in the
        codebase — no view, template, or other manager method computes one
        itself, so a total is simply unreachable through the query layer
        until voting closes, for any caller including the facilitator.
        """
        if cycle.voting_closed_at is None:
            raise VotingStillOpen(
                "Vote totals are not available until voting closes."
            )
        return dict(
            self.filter(cycle=cycle)
            .values("cluster")
            .annotate(total=models.Sum("weight"))
            .values_list("cluster", "total")
        )


VoteManager = models.Manager.from_queryset(VoteQuerySet)


class Vote(models.Model):
    """(cycle, member, cluster, weight) per stack.md. One row per
    (cycle, member, cluster) rather than one row per individual vote:
    ``weight`` holds how many of the member's three stackable votes are
    piled onto that cluster, so "add another vote to a cluster I've
    already voted on" is an update to an existing row, not a second insert
    that would need merging later. A row's weight is always >= 1 —
    retracting a cluster's last vote deletes the row instead of leaving a
    weight=0 husk behind (see projects.views.cast_vote).

    ``cluster`` is CASCADE, not SET_NULL like Card.cluster: a vote only
    ever means something in relation to the cluster it's on, so a deleted
    cluster's votes are meaningless and go with it — unlike a card, which
    can legitimately exist unclustered.
    """

    objects = VoteManager()

    cycle = models.ForeignKey(FeedbackCycle, on_delete=models.CASCADE, related_name="votes")
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="votes"
    )
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="votes")
    weight = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            # One row per (cycle, member, cluster): a second vote on a
            # cluster the member already voted on increments this row's
            # weight rather than creating a sibling row.
            models.UniqueConstraint(
                fields=["cycle", "member", "cluster"], name="unique_vote_per_member_cluster"
            ),
            # Belt-and-suspenders alongside the view-level cap check: a
            # weight can never be created or left at zero-or-below at the
            # database level either.
            models.CheckConstraint(
                condition=models.Q(weight__gte=1), name="vote_weight_at_least_one"
            ),
        ]

    def __str__(self):
        return f"{self.member} — {self.weight} vote(s) on {self.cluster}"


# -- #17: closing voting and ranking the discussion agenda --
#
# projects.views.close_voting is the one-shot facilitator action (per #6)
# that sets FeedbackCycle.voting_closed_at, which is also the point at
# which Vote.objects.totals_for_cycle (#16) stops raising VotingStillOpen.
# It uses that exact method to build the ranking below.

# -- #18: discussion mode --
#
# DiscussionTopic carries no direct FK to FeedbackCycle (only to Cluster,
# which itself points at the cycle), so every cycle-scoped query goes
# through cluster__cycle. Kept in a manager rather than repeated inline in
# projects.views, the same way Card.objects.visible_to (#10) and
# Vote.objects.totals_for_cycle (#16) centralize their own cycle-scoped
# logic.


class DiscussionTopicQuerySet(models.QuerySet):
    def for_cycle(self, *, cycle):
        """Every ``DiscussionTopic`` belonging to ``cycle`` — the full
        discussion agenda, in ``rank`` order (Meta's default ordering,
        left unchanged here).
        """
        return self.filter(cluster__cycle=cycle)

    def current_for_cycle(self, *, cycle):
        """The topic currently under discussion, per #18's decision:
        whichever ``DiscussionTopic`` in ``cycle`` has no ``outcome`` set
        yet and the lowest ``rank`` — the next undecided topic in agenda
        order. There is no separate "current topic" pointer field on any
        model (stack.md), so this is computed at query time, freshly, on
        every call. Returns ``None`` once every topic in the cycle has an
        outcome (or the cycle has no topics at all).
        """
        return self.for_cycle(cycle=cycle).filter(outcome="").order_by("rank").first()


DiscussionTopicManager = models.Manager.from_queryset(DiscussionTopicQuerySet)


class DiscussionTopic(models.Model):
    """(cluster, rank, outcome, notes) per stack.md — the prioritized
    discussion agenda produced when a facilitator closes voting (#17). One
    row is created for every cluster in the cycle, including a cluster with
    zero votes: nothing is dropped from the agenda, a zero-vote cluster
    simply sorts last. ``cluster`` is a one-to-one — closing voting is
    itself one-shot (rejected once ``voting_closed_at`` is already set), so
    a cluster never gets a second ``DiscussionTopic`` row.

    ``rank`` is stored rather than left as query-time ordering, since
    stack.md lists it as one of ``DiscussionTopic``'s own fields: 1 is the
    top of the agenda. projects.views.close_voting computes it by ordering
    clusters by vote total (from ``Vote.objects.totals_for_cycle``)
    descending, breaking ties by cluster creation order — earlier
    ``Cluster.pk`` first — since plan.md specifies vote-based ranking but
    not a tiebreak.

    ``outcome`` starts blank: nothing has been discussed yet at close-voting
    time. Setting it to discussed/skipped/deferred, and writing ``notes``,
    is #18's job.
    """

    class Outcome(models.TextChoices):
        DISCUSSED = "discussed", "Discussed"
        SKIPPED = "skipped", "Skipped"
        DEFERRED = "deferred", "Deferred"

    objects = DiscussionTopicManager()

    cluster = models.OneToOneField(
        Cluster, on_delete=models.CASCADE, related_name="discussion_topic"
    )
    rank = models.PositiveIntegerField()
    outcome = models.CharField(max_length=20, choices=Outcome.choices, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["rank"]

    def __str__(self):
        return f"#{self.rank} — {self.cluster.name}"


# -- #19: meeting upload page --
#
# MeetingRecord per stack.md: (cycle, kind, transcript_text,
# processing_state, task_id, error). Deliberately no file field anywhere on
# this model — per stack.md's no-persistent-media rule, an uploaded
# audio/video/transcript file exists only as a request-time upload; #19
# itself never writes it to disk (streaming it to a temp path for
# processing, then deleting it, is #20's job). A pasted-text submission has
# no file at all: transcript_text is populated directly from the form and
# processing_state is set straight to completed, since there's nothing left
# to process (see projects.views.meeting_upload).


class MeetingRecordQuerySet(models.QuerySet):
    def active_processing_for_cycle(self, *, cycle):
        """Every ``MeetingRecord`` in ``cycle`` whose ``processing_state`` is
        not yet terminal (pending or processing) — the query
        projects.views.meeting_upload uses to enforce #19's decision that
        only one MeetingRecord can be actively processing per cycle at a
        time. A pasted-text record is created with processing_state=completed
        directly (never pending/processing), so it is never returned here and
        never blocks a later upload — matching #19's explicit note that
        pasted text has nothing left "processing".
        """
        return self.filter(
            cycle=cycle,
            processing_state__in=[
                MeetingRecord.ProcessingState.PENDING,
                MeetingRecord.ProcessingState.PROCESSING,
            ],
        )


MeetingRecordManager = models.Manager.from_queryset(MeetingRecordQuerySet)


class MeetingRecord(models.Model):
    class Kind(models.TextChoices):
        AUDIO = "audio", "Audio"
        VIDEO = "video", "Video"
        TRANSCRIPT_FILE = "transcript_file", "Transcript file"
        PASTED_TEXT = "pasted_text", "Pasted text"

    class ProcessingState(models.TextChoices):
        # Not part of stack.md's field list itself (only the field name
        # ``processing_state`` is specified there) — this specific set of
        # choices is #19's own decision, kept deliberately simple since the
        # actual processing pipeline is #20's concern: a job sits pending
        # until a worker picks it up, moves to processing, and lands on
        # completed or failed. A pasted-text record skips straight to
        # completed (see MeetingRecordQuerySet.active_processing_for_cycle
        # above and the meeting_upload view).
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    objects = MeetingRecordManager()

    cycle = models.ForeignKey(
        FeedbackCycle, on_delete=models.CASCADE, related_name="meeting_records"
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    transcript_text = models.TextField(blank=True)
    processing_state = models.CharField(
        max_length=20, choices=ProcessingState.choices, default=ProcessingState.PENDING
    )
    # The string id django_q.tasks.async_task() returns, kept so a later view
    # (#20) can look the job up. Blank for a pasted_text record, which never
    # enqueues anything at all.
    task_id = models.CharField(max_length=100, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.get_kind_display()} meeting record for {self.cycle} "
            f"({self.processing_state})"
        )
