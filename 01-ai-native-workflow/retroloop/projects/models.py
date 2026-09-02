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
