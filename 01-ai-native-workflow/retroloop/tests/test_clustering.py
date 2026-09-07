"""Tests for #14: AI clustering suggestion.

Immediately after a facilitator's reveal action (#12) succeeds, revealed
cards are sent to the Anthropic API (tool-use) and the response is written
as ``Cluster`` rows with ``origin="suggested"``. These tests never call the
real Anthropic API: ``anthropic.Anthropic`` is mocked in every test that
exercises the clustering call, for both the success and failure paths.
"""
from unittest.mock import MagicMock, patch

import anthropic
import httpx2
import pytest
from django.urls import reverse

from projects.clustering import suggest_clusters_for_cycle
from projects.models import Card, Cluster, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


def _make_tool_use_response(clusters):
    """Build a fake anthropic.Anthropic().messages.create(...) return value
    carrying a single tool_use content block, shaped like the real SDK's
    parsed response (a ``.content`` list of blocks, each with ``.type`` and
    ``.input``).
    """
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.input = {"clusters": clusters}
    response = MagicMock()
    response.content = [tool_use_block]
    return response


@pytest.mark.django_db
class TestSuggestClustersForCycle:
    """Unit tests calling projects.clustering.suggest_clusters_for_cycle
    directly, against a mocked anthropic.Anthropic client.
    """

    def _make_cycle_with_cards(self, django_user_model, suffix=""):
        owner = django_user_model.objects.create_user(
            username=f"owner{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(name=f"Clustering Project {suffix}", created_by=owner)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.REVEALED
        )
        card1 = Card.objects.create(cycle=cycle, category="start", text="Card one", author=owner)
        card2 = Card.objects.create(cycle=cycle, category="stop", text="Card two", author=owner)
        card3 = Card.objects.create(
            cycle=cycle, category="continue", text="Card three", author=owner
        )
        return cycle, [card1, card2, card3]

    def test_successful_call_creates_suggested_clusters_covering_the_sent_cards(
        self, django_user_model
    ):
        cycle, (card1, card2, card3) = self._make_cycle_with_cards(django_user_model, "1")
        fake_response = _make_tool_use_response(
            [
                {"name": "Process issues", "card_ids": [card1.pk, card2.pk]},
                {"name": "Wins", "card_ids": [card3.pk]},
            ]
        )

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        clusters = list(Cluster.objects.filter(cycle=cycle).order_by("position"))
        assert len(clusters) == 2
        assert all(c.origin == Cluster.Origin.SUGGESTED for c in clusters)
        assert clusters[0].name == "Process issues"
        assert clusters[1].name == "Wins"

        card1.refresh_from_db()
        card2.refresh_from_db()
        card3.refresh_from_db()
        assert card1.cluster == clusters[0]
        assert card2.cluster == clusters[0]
        assert card3.cluster == clusters[1]

        # The call itself used tool-use, forced to our tool, on the pinned
        # model — per stack.md / the issue's constraints.
        _args, kwargs = mock_client.messages.create.call_args
        assert kwargs["model"] == "claude-sonnet-5"
        assert kwargs["tool_choice"] == {"type": "tool", "name": "suggest_clusters"}
        assert kwargs["tools"][0]["name"] == "suggest_clusters"
        assert kwargs["tools"][0]["strict"] is True

    def test_cards_left_out_of_every_group_stay_unclustered(self, django_user_model):
        cycle, (card1, card2, card3) = self._make_cycle_with_cards(django_user_model, "2")
        fake_response = _make_tool_use_response(
            [{"name": "Only group", "card_ids": [card1.pk]}]
        )

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        card2.refresh_from_db()
        card3.refresh_from_db()
        assert card2.cluster is None
        assert card3.cluster is None

    def test_failed_api_call_leaves_zero_clusters_and_all_cards_unclustered(
        self, django_user_model
    ):
        cycle, cards = self._make_cycle_with_cards(django_user_model, "3")

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            mock_client.messages.create.side_effect = anthropic.APIConnectionError(
                request=request
            )
            mock_anthropic_cls.return_value = mock_client

            # Must not raise.
            suggest_clusters_for_cycle(cycle)

        assert Cluster.objects.filter(cycle=cycle).count() == 0
        for card in cards:
            card.refresh_from_db()
            assert card.cluster is None

    def test_timeout_also_leaves_zero_clusters(self, django_user_model):
        cycle, cards = self._make_cycle_with_cards(django_user_model, "4")

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            mock_client.messages.create.side_effect = anthropic.APITimeoutError(request)
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        assert Cluster.objects.filter(cycle=cycle).count() == 0
        for card in cards:
            card.refresh_from_db()
            assert card.cluster is None

    def test_unnamed_cluster_from_the_response_is_skipped(self, django_user_model):
        cycle, (card1, _card2, _card3) = self._make_cycle_with_cards(django_user_model, "5")
        fake_response = _make_tool_use_response(
            [{"name": "  ", "card_ids": [card1.pk]}]
        )

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        assert Cluster.objects.filter(cycle=cycle).count() == 0
        card1.refresh_from_db()
        assert card1.cluster is None

    def test_hallucinated_card_id_is_dropped_but_real_ones_still_cluster(
        self, django_user_model
    ):
        cycle, (card1, _card2, _card3) = self._make_cycle_with_cards(django_user_model, "6")
        bogus_id = card1.pk + 999999
        fake_response = _make_tool_use_response(
            [{"name": "Mixed", "card_ids": [card1.pk, bogus_id]}]
        )

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        cluster = Cluster.objects.get(cycle=cycle)
        card1.refresh_from_db()
        assert card1.cluster == cluster
        assert cluster.cards.count() == 1

    def test_empty_card_ids_list_creates_no_cluster(self, django_user_model):
        cycle, _cards = self._make_cycle_with_cards(django_user_model, "7")
        fake_response = _make_tool_use_response([{"name": "Empty", "card_ids": []}])

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            suggest_clusters_for_cycle(cycle)

        assert Cluster.objects.filter(cycle=cycle).count() == 0

    def test_no_cards_in_cycle_does_not_call_the_api(self, django_user_model):
        owner = django_user_model.objects.create_user(
            username="owner_empty", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Empty Cycle Project", created_by=owner)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.REVEALED
        )

        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            suggest_clusters_for_cycle(cycle)
            mock_anthropic_cls.assert_not_called()

        assert Cluster.objects.filter(cycle=cycle).count() == 0


@pytest.mark.django_db
class TestRevealCycleTriggersClustering:
    """Integration tests going through the actual reveal_cycle view (#12),
    confirming clustering is wired in immediately after reveal and never
    blocks it.
    """

    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"revfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Reveal Clustering Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"revmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _reveal_url(self, project):
        return reverse("reveal_cycle", kwargs={"pk": project.pk})

    def test_successful_reveal_triggers_a_successful_clustering_call(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Do more of this", author=member
        )
        fake_response = _make_tool_use_response(
            [{"name": "Positives", "card_ids": [card.pk]}]
        )

        client.force_login(facilitator)
        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            mock_client.messages.create.return_value = fake_response
            mock_anthropic_cls.return_value = mock_client

            response = client.post(self._reveal_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.REVEALED
        assert cycle.revealed_at is not None

        clusters = list(Cluster.objects.filter(cycle=cycle))
        assert len(clusters) == 1
        assert clusters[0].origin == Cluster.Origin.SUGGESTED
        card.refresh_from_db()
        assert card.cluster == clusters[0]

    def test_failed_clustering_call_still_leaves_the_cycle_revealed(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        card = Card.objects.create(
            cycle=cycle, category="stop", text="Stop doing this", author=member
        )

        client.force_login(facilitator)
        with patch("projects.clustering.anthropic.Anthropic") as mock_anthropic_cls:
            mock_client = MagicMock()
            request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
            mock_client.messages.create.side_effect = anthropic.APIConnectionError(
                request=request
            )
            mock_anthropic_cls.return_value = mock_client

            response = client.post(self._reveal_url(project))

        # Reveal succeeds regardless — not an error response/state.
        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.REVEALED
        assert cycle.revealed_at is not None

        assert Cluster.objects.filter(cycle=cycle).count() == 0
        card.refresh_from_db()
        assert card.cluster is None

