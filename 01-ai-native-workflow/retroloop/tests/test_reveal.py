"""Tests for #12: the facilitator-only reveal action that transitions a
FeedbackCycle from "collecting" to "revealed".
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import Card, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestRevealCycle:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"fac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Reveal Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"mem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _reveal_url(self, project):
        return reverse("reveal_cycle", kwargs={"pk": project.pk})

    # -- Happy path --

    def test_facilitator_can_reveal_a_collecting_cycle(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="1"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.REVEALED
        assert cycle.revealed_at is not None
        assert cycle.revealed_at >= before

    # -- Facilitator-only, per #6 --

    def test_member_cannot_reveal_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="2"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(member)

        response = client.post(self._reveal_url(project))

        assert response.status_code == 404
        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.COLLECTING
        assert cycle.revealed_at is None

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="3"
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

        response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="4"
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(facilitator)

        response = client.get(self._reveal_url(project))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_reveal", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(reverse("reveal_cycle", kwargs={"pk": 999999}))

        assert response.status_code == 404

    # -- No re-reveal, no revealing out of order --

    @pytest.mark.parametrize(
        "state",
        [
            FeedbackCycle.State.REVEALED,
            FeedbackCycle.State.CLUSTERING,
            FeedbackCycle.State.VOTING,
            FeedbackCycle.State.DISCUSSING,
        ],
    )
    def test_reveal_is_rejected_when_cycle_is_not_collecting(
        self, client, django_user_model, state
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix=f"5{state}"
        )
        original_revealed_at = timezone.now()
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=state, revealed_at=original_revealed_at
        )
        client.force_login(facilitator)

        response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        # Unchanged: state stays exactly what it was, and revealed_at isn't
        # bumped to "now" by a second reveal attempt.
        assert cycle.state == state
        assert cycle.revealed_at == original_revealed_at

    def test_revealing_twice_does_not_update_revealed_at_a_second_time(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="6"
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(facilitator)

        client.post(self._reveal_url(project))
        cycle = FeedbackCycle.objects.get(project=project)
        first_revealed_at = cycle.revealed_at

        response = client.post(self._reveal_url(project))
        cycle.refresh_from_db()

        assert response.status_code == 302
        assert cycle.state == FeedbackCycle.State.REVEALED
        assert cycle.revealed_at == first_revealed_at

    def test_reveal_with_no_active_cycle_is_rejected(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="7"
        )
        client.force_login(facilitator)

        response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_reveal_with_only_a_closed_cycle_is_rejected(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="8"
        )
        closed_cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 0",
            state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
        )

        client.force_login(facilitator)
        response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        closed_cycle.refresh_from_db()
        assert closed_cycle.state == FeedbackCycle.State.CLOSED

    # -- Card creation/editing/withdrawal frozen after reveal, even for the author --

    def test_card_creation_is_rejected_after_reveal(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="9"
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(facilitator)
        client.post(self._reveal_url(project))

        client.force_login(member)
        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Too late", "anonymous": ""},
        )

        assert response.status_code == 200
        assert not Card.objects.exists()

    def test_editing_own_card_is_rejected_after_reveal(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="10"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Original", author=member
        )
        client.force_login(facilitator)
        client.post(self._reveal_url(project))

        client.force_login(member)
        response = client.post(
            reverse("edit_card", kwargs={"pk": project.pk, "card_id": card.pk}),
            {"category": "stop", "text": "Changed"},
        )

        assert response.status_code == 302
        card.refresh_from_db()
        assert card.text == "Original"
        assert card.category == "start"

    def test_withdrawing_own_card_is_rejected_after_reveal(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="11"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        card = Card.objects.create(cycle=cycle, category="start", text="Stay", author=member)
        client.force_login(facilitator)
        client.post(self._reveal_url(project))

        client.force_login(member)
        response = client.post(
            reverse("withdraw_card", kwargs={"pk": project.pk, "card_id": card.pk})
        )

        assert response.status_code == 302
        assert Card.objects.filter(pk=card.pk).exists()

    # -- Pre/post reveal visibility, driven through the actual reveal action --

    def test_member_b_cannot_see_member_as_card_before_reveal_but_can_after(
        self, client, django_user_model
    ):
        facilitator, member_a, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="12"
        )
        member_b = django_user_model.objects.create_user(
            username="memberb12", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member_b, role=Membership.Role.MEMBER)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        card_a = Card.objects.create(
            cycle=cycle, category="start", text="A's private thought", author=member_a
        )

        # Before reveal: member B can't reach it through Card.objects
        # .visible_to, the only view-facing query path (#10) — not even by
        # filtering directly on the known card ID.
        before = Card.objects.visible_to(cycle=cycle, viewer=member_b)
        assert card_a not in before
        assert not before.filter(pk=card_a.pk).exists()

        client.force_login(facilitator)
        response = client.post(self._reveal_url(project))
        assert response.status_code == 302

        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.REVEALED

        after = Card.objects.visible_to(cycle=cycle, viewer=member_b)
        assert card_a in after
