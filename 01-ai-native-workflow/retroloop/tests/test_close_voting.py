"""Tests for #17: the facilitator-only action that closes voting and
produces the prioritized discussion agenda.

Mirrors #12's reveal_cycle test file (tests/test_reveal.py) for the
facilitator-only, one-shot-guard pattern, and reuses #16's voting test
fixtures (tests/test_voting.py) for cycle/cluster/vote setup.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    Cluster,
    DiscussionTopic,
    FeedbackCycle,
    Membership,
    Project,
    Vote,
    VotingStillOpen,
)

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class CloseVotingTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"cvfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Close Voting Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"cvmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_revealed_cycle(self, project, voting_closed=False):
        return FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.VOTING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now() if voting_closed else None,
        )

    def _make_collecting_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

    def _close_voting_url(self, project):
        return reverse("close_voting", kwargs={"pk": project.pk})

    def _board_vote_url(self, project):
        return reverse("board_vote", kwargs={"pk": project.pk})


# -- Happy path: the facilitator-only action itself --


class TestCloseVotingAction(CloseVotingTestBase):
    def test_facilitator_can_close_voting_on_a_revealed_cycle(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.voting_closed_at is not None
        assert cycle.voting_closed_at >= before

    def test_member_cannot_close_voting_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(member)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 404
        cycle.refresh_from_db()
        assert cycle.voting_closed_at is None

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        self._make_revealed_cycle(project)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._close_voting_url(project))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_close_voting", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(reverse("close_voting", kwargs={"pk": 999999}))

        assert response.status_code == 404

    # -- Rejected if already closed (the required one-shot guard) --

    def test_closing_voting_twice_is_rejected_and_does_not_update_the_timestamp(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))
        cycle = FeedbackCycle.objects.get(project=project)
        first_closed_at = cycle.voting_closed_at

        response = client.post(self._close_voting_url(project))
        cycle.refresh_from_db()

        assert response.status_code == 302
        assert cycle.voting_closed_at == first_closed_at

    def test_closing_voting_twice_does_not_create_duplicate_discussion_topics(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_revealed_cycle(project)
        Cluster.objects.create(cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))
        client.post(self._close_voting_url(project))

        assert DiscussionTopic.objects.filter(cluster__cycle=cycle).count() == 1

    def test_closing_voting_when_already_closed_gets_rejected_even_with_no_clusters(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        self._make_revealed_cycle(project, voting_closed=True)
        client.force_login(facilitator)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        assert DiscussionTopic.objects.count() == 0

    # -- Rejected before reveal / with no reachable cycle --

    def test_closing_voting_before_reveal_is_rejected(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        cycle = self._make_collecting_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.voting_closed_at is None
        assert DiscussionTopic.objects.count() == 0

    def test_closing_voting_with_no_active_cycle_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        client.force_login(facilitator)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_closing_voting_with_only_a_closed_cycle_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        closed_cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 0",
            state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )
        client.force_login(facilitator)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        closed_cycle.refresh_from_db()
        assert DiscussionTopic.objects.count() == 0


# -- DiscussionTopic creation: every cluster, including zero-vote ones --


class TestDiscussionTopicCreation(CloseVotingTestBase):
    def test_a_discussion_topic_is_created_for_every_cluster_including_zero_votes(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        cycle = self._make_revealed_cycle(project)
        voted_cluster = Cluster.objects.create(
            cycle=cycle, name="Voted", origin=Cluster.Origin.SUGGESTED
        )
        zero_vote_cluster = Cluster.objects.create(
            cycle=cycle, name="No votes", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=voted_cluster, weight=2)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))

        assert DiscussionTopic.objects.filter(cluster=voted_cluster).exists()
        assert DiscussionTopic.objects.filter(cluster=zero_vote_cluster).exists()
        assert DiscussionTopic.objects.filter(cluster__cycle=cycle).count() == 2

    def test_a_cycle_with_no_clusters_at_all_still_closes_cleanly(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._close_voting_url(project))

        assert response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.voting_closed_at is not None
        assert DiscussionTopic.objects.count() == 0

    def test_new_discussion_topics_start_with_a_blank_outcome_and_notes(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "13"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))

        topic = DiscussionTopic.objects.get(cluster=cluster)
        assert topic.outcome == ""
        assert topic.notes == ""


# -- Ranking: vote total descending, ties broken by cluster creation order --


class TestDiscussionTopicRanking(CloseVotingTestBase):
    def test_order_matches_vote_totals_descending(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "14"
        )
        cycle = self._make_revealed_cycle(project)
        low = Cluster.objects.create(cycle=cycle, name="Low", origin=Cluster.Origin.SUGGESTED)
        high = Cluster.objects.create(cycle=cycle, name="High", origin=Cluster.Origin.SUGGESTED)
        mid = Cluster.objects.create(cycle=cycle, name="Mid", origin=Cluster.Origin.SUGGESTED)
        Vote.objects.create(cycle=cycle, member=member, cluster=low, weight=1)
        Vote.objects.create(cycle=cycle, member=member, cluster=high, weight=3)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=mid, weight=2)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))

        ranks = {
            topic.cluster_id: topic.rank
            for topic in DiscussionTopic.objects.filter(cluster__cycle=cycle)
        }
        assert ranks[high.pk] < ranks[mid.pk] < ranks[low.pk]
        # 1-indexed, one rank per cluster, no gaps.
        assert sorted(ranks.values()) == [1, 2, 3]

    def test_ties_are_broken_by_cluster_creation_order_earlier_cluster_first(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "15"
        )
        cycle = self._make_revealed_cycle(project)
        # Created in this order; all end up with equal vote totals (2 each).
        first = Cluster.objects.create(
            cycle=cycle, name="First", origin=Cluster.Origin.SUGGESTED
        )
        second = Cluster.objects.create(
            cycle=cycle, name="Second", origin=Cluster.Origin.SUGGESTED
        )
        third = Cluster.objects.create(
            cycle=cycle, name="Third", origin=Cluster.Origin.SUGGESTED
        )
        assert first.pk < second.pk < third.pk
        Vote.objects.create(cycle=cycle, member=member, cluster=third, weight=2)
        Vote.objects.create(cycle=cycle, member=member, cluster=first, weight=1)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=first, weight=1)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=second, weight=2)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))

        ranks = {
            topic.cluster_id: topic.rank
            for topic in DiscussionTopic.objects.filter(cluster__cycle=cycle)
        }
        # All three tied at a total of 2 — earlier Cluster.pk sorts first.
        assert ranks[first.pk] == 1
        assert ranks[second.pk] == 2
        assert ranks[third.pk] == 3

    def test_a_zero_vote_cluster_sorts_last_rather_than_being_dropped(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "16"
        )
        cycle = self._make_revealed_cycle(project)
        zero_vote_cluster = Cluster.objects.create(
            cycle=cycle, name="No votes", origin=Cluster.Origin.SUGGESTED
        )
        voted_cluster = Cluster.objects.create(
            cycle=cycle, name="Voted", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=voted_cluster, weight=1)
        client.force_login(facilitator)

        client.post(self._close_voting_url(project))

        zero_vote_topic = DiscussionTopic.objects.get(cluster=zero_vote_cluster)
        voted_topic = DiscussionTopic.objects.get(cluster=voted_cluster)
        assert voted_topic.rank < zero_vote_topic.rank
        assert zero_vote_topic.rank == 2


# -- The explicitly required test: after closing, totals are visible where
# #16 asserted they weren't. --


class TestTotalsVisibleAfterClosing(CloseVotingTestBase):
    def test_totals_for_cycle_no_longer_raises_after_closing(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "17"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        client.force_login(facilitator)

        with pytest.raises(VotingStillOpen):
            Vote.objects.totals_for_cycle(cycle=cycle)

        client.post(self._close_voting_url(project))
        cycle.refresh_from_db()

        totals = Vote.objects.totals_for_cycle(cycle=cycle)
        assert totals == {cluster.pk: 2}

    def test_every_member_not_just_the_facilitator_sees_the_total_on_board_vote(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "18"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Contested theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=cluster, weight=1)

        client.force_login(facilitator)
        client.post(self._close_voting_url(project))

        for viewer in (member, facilitator):
            client.force_login(viewer)
            response = client.get(self._board_vote_url(project))
            content = response.content.decode()

            assert response.status_code == 200
            assert "Total votes: 3" in content

    def test_totals_still_hidden_on_board_vote_before_closing(
        self, client, django_user_model
    ):
        # Sanity check the negative case still holds up to the moment of
        # closing (mirrors #16's own
        # test_no_view_exposes_a_vote_total_while_voting_is_open).
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "19"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        client.force_login(member)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert "Total votes" not in content

    def test_close_voting_button_only_shown_to_the_facilitator(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        self._make_revealed_cycle(project)

        client.force_login(member)
        member_response = client.get(self._board_vote_url(project))
        client.force_login(facilitator)
        facilitator_response = client.get(self._board_vote_url(project))

        assert "Close voting" not in member_response.content.decode()
        assert "Close voting" in facilitator_response.content.decode()
