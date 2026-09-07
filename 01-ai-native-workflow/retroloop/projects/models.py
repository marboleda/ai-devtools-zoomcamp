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
