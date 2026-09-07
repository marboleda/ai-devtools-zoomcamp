"""Tests for #16: casting and reallocating votes on clusters — three
stackable votes per member per cycle, freely reallocatable at any time
before ``voting_closed_at`` is set, with the 3-vote cap enforced
server-side and cluster vote totals unreachable through any query path
until voting closes (mirrors #10's ``Card.objects.visible_to`` pattern via
``Vote.objects.totals_for_cycle``).
"""
import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    MAX_VOTE_WEIGHT_PER_MEMBER,
    Card,
    Cluster,
    FeedbackCycle,
    Membership,
    Project,
    Vote,
    VotingStillOpen,
)

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class VotingTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"vfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Voting Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"vmem{suffix}", password=VALID_PASSWORD
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

    def _board_vote_url(self, project):
        return reverse("board_vote", kwargs={"pk": project.pk})

    def _cast_url(self, project, cluster):
        return reverse("cast_vote", kwargs={"pk": project.pk, "cluster_id": cluster.pk})


# -- Model/manager layer: the privacy gate and the plain per-member reads,
# called directly (not through a view), per #16's explicit test requirement
# that Vote.objects.totals_for_cycle is exercised on its own.


class TestVoteTotalsForCycleGate(VotingTestBase):
    def test_totals_for_cycle_raises_while_voting_is_open(self, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)

        with pytest.raises(VotingStillOpen):
            Vote.objects.totals_for_cycle(cycle=cycle)

    def test_totals_for_cycle_raises_even_for_a_cycle_with_no_votes_at_all(
        self, django_user_model
    ):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_revealed_cycle(project)

        with pytest.raises(VotingStillOpen):
            Vote.objects.totals_for_cycle(cycle=cycle)

    def test_totals_for_cycle_returns_correct_sums_once_voting_closed(
        self, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        cluster_a = Cluster.objects.create(
            cycle=cycle, name="A", origin=Cluster.Origin.SUGGESTED
        )
        cluster_b = Cluster.objects.create(
            cycle=cycle, name="B", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster_a, weight=2)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=cluster_a, weight=1)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=cluster_b, weight=1)

        totals = Vote.objects.totals_for_cycle(cycle=cycle)

        assert totals == {cluster_a.pk: 3, cluster_b.pk: 1}

    def test_totals_for_cycle_gate_applies_regardless_of_caller_role(
        self, django_user_model
    ):
        # The gate is keyed on cycle.voting_closed_at only — it takes no
        # viewer/role argument at all, so there is no way to pass a
        # facilitator flag to get an early answer.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=3)

        with pytest.raises(VotingStillOpen):
            Vote.objects.totals_for_cycle(cycle=cycle)


class TestVoteForMemberInCycle(VotingTestBase):
    def test_for_member_in_cycle_returns_only_that_members_own_rows(
        self, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        member_vote = Vote.objects.create(
            cycle=cycle, member=member, cluster=cluster, weight=2
        )
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=cluster, weight=1)

        visible = Vote.objects.for_member_in_cycle(cycle=cycle, member=member)

        assert set(visible) == {member_vote}

    def test_total_weight_for_member_in_cycle_sums_across_clusters(
        self, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_revealed_cycle(project)
        cluster_a = Cluster.objects.create(
            cycle=cycle, name="A", origin=Cluster.Origin.SUGGESTED
        )
        cluster_b = Cluster.objects.create(
            cycle=cycle, name="B", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster_a, weight=2)
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster_b, weight=1)

        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)

        assert total == 3

    def test_total_weight_for_member_in_cycle_is_zero_with_no_votes(
        self, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        cycle = self._make_revealed_cycle(project)

        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)

        assert total == 0


class TestVoteModelConstraints(VotingTestBase):
    def test_duplicate_member_cluster_row_is_rejected_at_the_database_level(
        self, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=1)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=1)

    def test_zero_weight_is_rejected_at_the_database_level(self, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=0)


# -- board_vote view: renders only the viewer's own allocation, never a
# cross-member total, while voting is open.


class TestBoardVoteView(VotingTestBase):
    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_bv1", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.get(self._board_vote_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )

        response = client.get(self._board_vote_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_bv", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("board_vote", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_before_reveal_shows_not_revealed_message(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        self._make_collecting_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "haven't been revealed" in content

    def test_no_active_cycle_renders_without_crashing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "13"
        )
        client.force_login(facilitator)

        response = client.get(self._board_vote_url(project))

        assert response.status_code == 200

    def test_ordinary_member_not_just_facilitator_can_view(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "14"
        )
        cycle = self._make_revealed_cycle(project)
        Cluster.objects.create(cycle=cycle, name="Theme A", origin=Cluster.Origin.SUGGESTED)
        client.force_login(member)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Theme A" in content

    def test_shows_own_vote_count_and_remaining_votes(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "15"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme A", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        client.force_login(member)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert "Your votes here: 2" in content
        assert "You have 1 of 3 votes left" in content

    def test_htmx_request_returns_only_the_fragment_not_the_full_page(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "16"
        )
        cycle = self._make_revealed_cycle(project)
        Cluster.objects.create(cycle=cycle, name="Polled cluster", origin=Cluster.Origin.SUGGESTED)
        client.force_login(facilitator)

        response = client.get(self._board_vote_url(project), HTTP_HX_REQUEST="true")
        content = response.content.decode()

        assert response.status_code == 200
        assert "Polled cluster" in content
        assert "htmx.org" not in content
        assert "Back to project" not in content

    def test_non_htmx_request_returns_the_full_page(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "17"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert "htmx.org" in content
        assert "Back to project" in content

    def test_page_carries_htmx_polling_markup(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "18"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_vote_url(project))
        content = response.content.decode()

        assert 'hx-trigger="every 3s"' in content
        assert f'hx-get="{self._board_vote_url(project)}"' in content

    # -- The explicitly required privacy test: no view exposes a vote
    # total to any member while voting_closed_at is unset. --

    def test_no_view_exposes_a_vote_total_while_voting_is_open(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "19"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Contested theme", origin=Cluster.Origin.SUGGESTED
        )
        # Two members pile votes onto the same cluster — a combined total
        # of 3 — and neither viewer, including the facilitator, should see
        # any number resembling that combined total anywhere on the board.
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        Vote.objects.create(cycle=cycle, member=facilitator, cluster=cluster, weight=1)

        for viewer in (member, facilitator):
            client.force_login(viewer)
            vote_response = client.get(self._board_vote_url(project))
            cluster_response = client.get(
                reverse("board_cluster", kwargs={"pk": project.pk})
            )
            reveal_response = client.get(
                reverse("board_reveal", kwargs={"pk": project.pk})
            )

            for response in (vote_response, cluster_response, reveal_response):
                content = response.content.decode()
                # The combined total (3) never appears attributed to the
                # cluster; each viewer only ever sees their own weight (2
                # for member, 1 for facilitator), never the other's or the
                # sum.
                assert "Your votes here: 3" not in content
                assert "total" not in content.lower()

        # And the manager method backing any future totals display raises
        # rather than returning the combined 3, for either viewer's role.
        with pytest.raises(VotingStillOpen):
            Vote.objects.totals_for_cycle(cycle=cycle)


# -- cast_vote: adding, retracting, reallocating, and the server-side cap.


class TestCastVote(VotingTestBase):
    def test_member_can_add_a_vote_to_a_cluster(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 200
        vote = Vote.objects.get(cycle=cycle, member=member, cluster=cluster)
        assert vote.weight == 1

    def test_member_can_stack_multiple_votes_on_the_same_cluster(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        client.post(self._cast_url(project, cluster), {"delta": "1"})
        client.post(self._cast_url(project, cluster), {"delta": "1"})
        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 200
        vote = Vote.objects.get(cycle=cycle, member=member, cluster=cluster)
        assert vote.weight == 3

    def test_member_can_retract_a_vote(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=2)
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "-1"})

        assert response.status_code == 200
        vote = Vote.objects.get(cycle=cycle, member=member, cluster=cluster)
        assert vote.weight == 1

    def test_retracting_a_clusters_last_vote_deletes_the_row(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "23"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster, weight=1)
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "-1"})

        assert response.status_code == 200
        assert not Vote.objects.filter(cycle=cycle, member=member, cluster=cluster).exists()

    def test_retracting_from_a_cluster_with_no_vote_is_a_no_op(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "24"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "-1"})

        assert response.status_code == 200
        assert not Vote.objects.filter(cycle=cycle, member=member, cluster=cluster).exists()

    def test_member_can_move_a_vote_between_clusters(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "25"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=source, weight=1)
        client.force_login(member)

        client.post(self._cast_url(project, source), {"delta": "-1"})
        client.post(self._cast_url(project, target), {"delta": "1"})

        assert not Vote.objects.filter(cycle=cycle, member=member, cluster=source).exists()
        assert Vote.objects.get(cycle=cycle, member=member, cluster=target).weight == 1
        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)
        assert total == 1

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "26"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_cv", password=VALID_PASSWORD
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(outsider)

        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 404
        assert not Vote.objects.filter(cycle=cycle).exists()

    def test_vote_before_reveal_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "27"
        )
        cycle = self._make_collecting_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 404

    def test_vote_after_voting_closed_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "28"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 404
        assert not Vote.objects.filter(cycle=cycle).exists()

    def test_get_is_not_allowed(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "29"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.get(self._cast_url(project, cluster))

        assert response.status_code == 405

    def test_cluster_from_another_cycle_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "30"
        )
        self._make_revealed_cycle(project)
        other_owner = django_user_model.objects.create_user(
            username="other_owner_cv", password=VALID_PASSWORD
        )
        other_project = Project.objects.create(name="Other project", created_by=other_owner)
        other_cycle = FeedbackCycle.objects.create(
            project=other_project,
            week="Cycle 1",
            state=FeedbackCycle.State.VOTING,
            revealed_at=timezone.now(),
        )
        foreign_cluster = Cluster.objects.create(
            cycle=other_cycle, name="Not yours", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._cast_url(project, foreign_cluster), {"delta": "1"})

        assert response.status_code == 404

    # -- Cards left unclustered cannot receive votes directly: there is no
    # endpoint that accepts a card id at all — casting a vote always names
    # a Cluster, and an id that doesn't resolve to a Cluster in this cycle
    # (e.g. a card's own id) 404s the same as any other bad cluster id.

    def test_a_card_id_cannot_be_used_to_vote_directly(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "31"
        )
        cycle = self._make_revealed_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Unclustered card", author=member
        )
        assert card.cluster_id is None
        client.force_login(member)

        url = reverse("cast_vote", kwargs={"pk": project.pk, "cluster_id": card.pk})
        response = client.post(url, {"delta": "1"})

        assert response.status_code == 404
        assert not Vote.objects.filter(cycle=cycle, member=member).exists()

    # -- The explicitly required cap test: a manipulated payload cannot
    # push a member's total vote weight above 3, even bypassing the UI's
    # disabled button entirely by posting straight to the endpoint. --

    def test_a_manipulated_delta_value_cannot_jump_by_more_than_one_vote(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "32"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        for crafted_delta in ("5", "3", "999", "0", "", "abc"):
            response = client.post(
                self._cast_url(project, cluster), {"delta": crafted_delta}
            )
            assert response.status_code == 404

        assert not Vote.objects.filter(cycle=cycle, member=member).exists()

    def test_a_fourth_vote_is_rejected_once_three_are_already_spent_on_one_cluster(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "33"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        for _ in range(3):
            client.post(self._cast_url(project, cluster), {"delta": "1"})
        response = client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert response.status_code == 200
        vote = Vote.objects.get(cycle=cycle, member=member, cluster=cluster)
        assert vote.weight == 3
        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)
        assert total == MAX_VOTE_WEIGHT_PER_MEMBER

    def test_a_fourth_vote_is_rejected_when_spread_across_different_clusters(
        self, client, django_user_model
    ):
        # The cap is on the member's total across the whole cycle, not
        # per-cluster: 3 spent across two clusters still blocks a 4th
        # anywhere, including a brand new cluster.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "34"
        )
        cycle = self._make_revealed_cycle(project)
        cluster_a = Cluster.objects.create(
            cycle=cycle, name="A", origin=Cluster.Origin.SUGGESTED
        )
        cluster_b = Cluster.objects.create(
            cycle=cycle, name="B", origin=Cluster.Origin.SUGGESTED
        )
        cluster_c = Cluster.objects.create(
            cycle=cycle, name="C", origin=Cluster.Origin.SUGGESTED
        )
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster_a, weight=2)
        Vote.objects.create(cycle=cycle, member=member, cluster=cluster_b, weight=1)
        client.force_login(member)

        response = client.post(self._cast_url(project, cluster_c), {"delta": "1"})

        assert response.status_code == 200
        assert not Vote.objects.filter(
            cycle=cycle, member=member, cluster=cluster_c
        ).exists()
        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)
        assert total == MAX_VOTE_WEIGHT_PER_MEMBER

    def test_a_flood_of_add_requests_never_exceeds_the_cap(
        self, client, django_user_model
    ):
        # Simulates a scripted/manipulated client that ignores the
        # disabled button and just keeps posting: the cap holds regardless
        # of how many add requests arrive.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "35"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        for _ in range(10):
            client.post(self._cast_url(project, cluster), {"delta": "1"})

        total = Vote.objects.total_weight_for_member_in_cycle(cycle=cycle, member=member)
        assert total == MAX_VOTE_WEIGHT_PER_MEMBER

    def test_votes_are_per_member_each_member_gets_their_own_three(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "36"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )

        client.force_login(member)
        for _ in range(3):
            client.post(self._cast_url(project, cluster), {"delta": "1"})

        client.force_login(facilitator)
        for _ in range(3):
            client.post(self._cast_url(project, cluster), {"delta": "1"})

        assert Vote.objects.get(cycle=cycle, member=member, cluster=cluster).weight == 3
        assert Vote.objects.get(cycle=cycle, member=facilitator, cluster=cluster).weight == 3
