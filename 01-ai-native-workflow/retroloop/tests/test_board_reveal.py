"""Tests for #13: the retrospective board's Reveal mode — every card for a
revealed-or-later cycle, grouped by category only (no clustering), with
anonymous cards showing no author.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import Card, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestBoardReveal:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"fac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Board Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"mem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _board_url(self, project):
        return reverse("board_reveal", kwargs={"pk": project.pk})

    # -- Non-member / auth gating, per #5/#6 --

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="1"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider1", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.get(self._board_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="2"
        )

        response = client.get(self._board_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_board", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("board_reveal", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_ordinary_member_not_just_facilitator_can_view(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="3"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        Card.objects.create(cycle=cycle, category="start", text="Visible", author=facilitator)
        client.force_login(member)

        response = client.get(self._board_url(project))

        assert response.status_code == 200
        assert "Visible" in response.content.decode()

    # -- Pre-reveal: no card content is exposed --

    def test_before_reveal_no_cards_are_listed_even_to_a_member(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="4"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        Card.objects.create(
            cycle=cycle, category="start", text="A private thought", author=member
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))

        assert response.status_code == 200
        assert "A private thought" not in response.content.decode()

    def test_no_active_cycle_renders_without_crashing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="5"
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))

        assert response.status_code == 200

    def test_only_a_closed_cycle_renders_without_crashing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="6"
        )
        FeedbackCycle.objects.create(
            project=project,
            week="Cycle 0",
            state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))

        assert response.status_code == 200

    # -- Reveal-or-later, ungrouped by cluster, regardless of stage --

    @pytest.mark.parametrize(
        "state",
        [
            FeedbackCycle.State.REVEALED,
            FeedbackCycle.State.CLUSTERING,
            FeedbackCycle.State.VOTING,
            FeedbackCycle.State.DISCUSSING,
        ],
    )
    def test_cards_are_listed_at_and_after_reveal_regardless_of_stage(
        self, client, django_user_model, state
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix=f"7{state}"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=state, revealed_at=timezone.now()
        )
        Card.objects.create(
            cycle=cycle, category="start", text="Start thing", author=member
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))

        assert response.status_code == 200
        assert "Start thing" in response.content.decode()

    def test_cards_are_grouped_by_category_start_stop_continue(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="8"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        Card.objects.create(cycle=cycle, category="start", text="Start card", author=member)
        Card.objects.create(cycle=cycle, category="stop", text="Stop card", author=member)
        Card.objects.create(
            cycle=cycle, category="continue", text="Continue card", author=member
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        start_index = content.index("Start")
        start_card_index = content.index("Start card")
        stop_index = content.index("Stop")
        stop_card_index = content.index("Stop card")
        continue_index = content.index("Continue")
        continue_card_index = content.index("Continue card")
        # Each card appears under its own category heading, in start / stop
        # / continue order.
        assert start_index < start_card_index < stop_index < stop_card_index
        assert stop_card_index < continue_index < continue_card_index

    def test_card_with_no_cluster_still_appears_ungrouped_by_cluster(
        self, client, django_user_model
    ):
        # No clustering UI exists yet (out of scope, #14), but this pins the
        # rule for when it does: this view never reads Card.cluster.
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="9"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Unclustered card", author=member
        )
        assert card.cluster_id is None
        client.force_login(facilitator)

        response = client.get(self._board_url(project))

        assert "Unclustered card" in response.content.decode()

    # -- Anonymity --

    def test_anonymous_card_shows_no_author(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="10"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        Card.objects.create(
            cycle=cycle,
            category="start",
            text="Secret feedback",
            author=None,
            edit_token_hash="deadbeef",
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))
        content = response.content.decode()

        assert "Secret feedback" in content
        assert member.username not in content
        assert facilitator.username not in content
        assert "Anonymous" in content

    def test_attributed_card_shows_authors_username(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="11"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        Card.objects.create(
            cycle=cycle, category="start", text="Attributed feedback", author=member
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))
        content = response.content.decode()

        assert "Attributed feedback" in content
        assert member.username in content

    # -- htmx polling, per stack.md's ~3s decision --

    def test_page_carries_htmx_polling_markup(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="12"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))
        content = response.content.decode()

        assert 'hx-trigger="every 3s"' in content
        assert f'hx-get="{self._board_url(project)}"' in content

    def test_htmx_request_returns_only_the_fragment_not_the_full_page(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="13"
        )
        cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        Card.objects.create(cycle=cycle, category="start", text="Polled card", author=member)
        client.force_login(facilitator)

        response = client.get(self._board_url(project), HTTP_HX_REQUEST="true")
        content = response.content.decode()

        assert response.status_code == 200
        assert "Polled card" in content
        # Only the polling target re-renders on a poll: the page chrome
        # (heading, htmx script tag, "back" link) is not part of the
        # fragment response.
        assert "htmx.org" not in content
        assert "Back to project" not in content
        assert f"<h1>Reveal — {project.name}</h1>" not in content

    def test_non_htmx_request_returns_the_full_page(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, suffix="14"
        )
        FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        client.force_login(facilitator)

        response = client.get(self._board_url(project))
        content = response.content.decode()

        assert "htmx.org" in content
        assert "Back to project" in content
