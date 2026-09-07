"""Tests for #15: manual clustering on the retrospective board's Cluster
mode — any member (not just the facilitator) can drag a card to a
different cluster or to unclustered, merge two clusters, split a cluster,
and rename any cluster regardless of its origin. Changes are visible to
every member through the same htmx poll #13 already wires up.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import Card, Cluster, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestBoardClusterBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"cfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Cluster Board Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"cmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_revealed_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.CLUSTERING,
            revealed_at=timezone.now(),
        )

    def _make_collecting_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

    def _board_cluster_url(self, project):
        return reverse("board_cluster", kwargs={"pk": project.pk})

    def _move_url(self, project, card):
        return reverse("move_card", kwargs={"pk": project.pk, "card_id": card.pk})

    def _rename_url(self, project, cluster):
        return reverse("rename_cluster", kwargs={"pk": project.pk, "cluster_id": cluster.pk})

    def _merge_url(self, project, source):
        return reverse("merge_clusters", kwargs={"pk": project.pk, "cluster_id": source.pk})

    def _split_url(self, project, cluster):
        return reverse("split_cluster", kwargs={"pk": project.pk, "cluster_id": cluster.pk})


class TestBoardClusterView(TestBoardClusterBase):
    # -- Membership / auth gating, same convention as board_reveal --

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_cb1", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.get(self._board_cluster_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )

        response = client.get(self._board_cluster_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_cb", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("board_cluster", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_ordinary_member_not_just_facilitator_can_view(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme A", origin=Cluster.Origin.SUGGESTED
        )
        Card.objects.create(
            cycle=cycle, category="start", text="Clustered card",
            author=facilitator, cluster=cluster,
        )
        client.force_login(member)

        response = client.get(self._board_cluster_url(project))

        assert response.status_code == 200
        assert "Clustered card" in response.content.decode()
        assert "Theme A" in response.content.decode()

    def test_before_reveal_shows_not_revealed_message(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        self._make_collecting_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "haven't been revealed" in content

    def test_no_active_cycle_renders_without_crashing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project))

        assert response.status_code == 200

    def test_unclustered_card_appears_under_unclustered(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_revealed_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Loose card", author=member
        )
        assert card.cluster_id is None
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project))
        content = response.content.decode()

        assert "Loose card" in content
        assert "Unclustered" in content

    # -- htmx polling, same shared mechanism as board_reveal (#13) --

    def test_page_carries_htmx_polling_markup(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project))
        content = response.content.decode()

        assert 'hx-trigger="every 3s"' in content
        assert f'hx-get="{self._board_cluster_url(project)}"' in content

    def test_htmx_request_returns_only_the_fragment_not_the_full_page(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        cycle = self._make_revealed_cycle(project)
        Card.objects.create(cycle=cycle, category="start", text="Polled card", author=member)
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project), HTTP_HX_REQUEST="true")
        content = response.content.decode()

        assert response.status_code == 200
        assert "Polled card" in content
        assert "htmx.org" not in content
        assert "Back to project" not in content

    def test_non_htmx_request_returns_the_full_page(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_cluster_url(project))
        content = response.content.decode()

        assert "htmx.org" in content
        assert "sortablejs" in content
        assert "Back to project" in content

    # -- Sharing: one member's edits are visible to another via this same view --

    def test_another_members_move_is_visible_on_this_members_next_load(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme A", origin=Cluster.Origin.SUGGESTED
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Shared card", author=member
        )

        client.force_login(member)
        client.post(self._move_url(project, card), {"cluster_id": cluster.pk})

        # A different member polling the same URL sees the move too.
        client.force_login(facilitator)
        response = client.get(self._board_cluster_url(project), HTTP_HX_REQUEST="true")
        content = response.content.decode()

        card_index = content.index("Shared card")
        theme_index = content.index("Theme A")
        unclustered_index = content.index("Unclustered")
        assert theme_index < card_index < unclustered_index


class TestMoveCard(TestBoardClusterBase):
    def test_any_member_can_move_a_card_to_a_different_cluster(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_revealed_cycle(project)
        source_cluster = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        target_cluster = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.SUGGESTED
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Movable", author=member,
            cluster=source_cluster,
        )
        client.force_login(member)

        response = client.post(
            self._move_url(project, card), {"cluster_id": target_cluster.pk}
        )

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.cluster_id == target_cluster.pk

    def test_moving_a_card_to_unclustered_clears_its_cluster(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.HUMAN
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Goes loose", author=member,
            cluster=cluster,
        )
        client.force_login(member)

        response = client.post(self._move_url(project, card), {"cluster_id": ""})

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.cluster_id is None

    def test_move_works_the_same_whether_source_is_suggested_or_human(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_revealed_cycle(project)
        human_cluster = Cluster.objects.create(
            cycle=cycle, name="Human", origin=Cluster.Origin.HUMAN
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.SUGGESTED
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Move me", author=member,
            cluster=human_cluster,
        )
        client.force_login(member)

        response = client.post(self._move_url(project, card), {"cluster_id": target.pk})

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.cluster_id == target.pk
        # Moving a single card never changes anyone's origin.
        human_cluster.refresh_from_db()
        target.refresh_from_db()
        assert human_cluster.origin == Cluster.Origin.HUMAN
        assert target.origin == Cluster.Origin.SUGGESTED

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "23"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_mv", password=VALID_PASSWORD
        )
        cycle = self._make_revealed_cycle(project)
        card = Card.objects.create(cycle=cycle, category="start", text="X", author=member)
        client.force_login(outsider)

        response = client.post(self._move_url(project, card), {"cluster_id": ""})

        assert response.status_code == 404

    def test_card_from_another_cycle_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "24"
        )
        self._make_revealed_cycle(project)
        other_owner = django_user_model.objects.create_user(
            username="other_owner_mv", password=VALID_PASSWORD
        )
        other_project = Project.objects.create(name="Other project", created_by=other_owner)
        other_cycle = FeedbackCycle.objects.create(
            project=other_project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED,
            revealed_at=timezone.now(),
        )
        foreign_card = Card.objects.create(
            cycle=other_cycle, category="start", text="Not yours", author=other_owner
        )
        client.force_login(member)

        response = client.post(self._move_url(project, foreign_card), {"cluster_id": ""})

        assert response.status_code == 404

    def test_move_before_reveal_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "25"
        )
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(cycle=cycle, category="start", text="X", author=member)
        client.force_login(member)

        response = client.post(self._move_url(project, card), {"cluster_id": ""})

        assert response.status_code == 404

    def test_get_is_not_allowed(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "26"
        )
        cycle = self._make_revealed_cycle(project)
        card = Card.objects.create(cycle=cycle, category="start", text="X", author=member)
        client.force_login(member)

        response = client.get(self._move_url(project, card))

        assert response.status_code == 405


class TestRenameCluster(TestBoardClusterBase):
    @pytest.mark.parametrize("origin", [Cluster.Origin.SUGGESTED, Cluster.Origin.HUMAN])
    def test_any_member_can_rename_any_cluster_regardless_of_origin(
        self, client, django_user_model, origin
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, f"30{origin}"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(cycle=cycle, name="Old name", origin=origin)
        client.force_login(member)

        response = client.post(self._rename_url(project, cluster), {"name": "New name"})

        assert response.status_code == 200
        cluster.refresh_from_db()
        assert cluster.name == "New name"
        # Renaming never touches origin.
        assert cluster.origin == origin

    def test_blank_name_is_rejected_and_leaves_the_cluster_unchanged(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "31"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Keep me", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._rename_url(project, cluster), {"name": "   "})

        assert response.status_code == 200
        cluster.refresh_from_db()
        assert cluster.name == "Keep me"

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "32"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_rn", password=VALID_PASSWORD
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(outsider)

        response = client.post(self._rename_url(project, cluster), {"name": "Hijacked"})

        assert response.status_code == 404


class TestMergeClusters(TestBoardClusterBase):
    def test_merging_two_clusters_leaves_one_cluster_with_the_union_of_both_clusters_cards(
        self, client, django_user_model
    ):
        # The acceptance criterion this issue explicitly calls out.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "40"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source theme", origin=Cluster.Origin.SUGGESTED
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target theme", origin=Cluster.Origin.SUGGESTED
        )
        source_card_1 = Card.objects.create(
            cycle=cycle, category="start", text="S1", author=member, cluster=source
        )
        source_card_2 = Card.objects.create(
            cycle=cycle, category="stop", text="S2", author=member, cluster=source
        )
        target_card = Card.objects.create(
            cycle=cycle, category="continue", text="T1", author=member, cluster=target
        )
        client.force_login(member)

        response = client.post(
            self._merge_url(project, source), {"target_cluster_id": target.pk}
        )

        assert response.status_code == 200
        remaining = list(Cluster.objects.filter(cycle=cycle))
        assert len(remaining) == 1
        merged = remaining[0]
        assert merged.pk == target.pk
        assert set(merged.cards.all()) == {source_card_1, source_card_2, target_card}
        assert not Cluster.objects.filter(pk=source.pk).exists()

    def test_merged_cluster_keeps_the_drop_targets_name_and_becomes_human_origin(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "41"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source name", origin=Cluster.Origin.HUMAN
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target name", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        client.post(self._merge_url(project, source), {"target_cluster_id": target.pk})

        target.refresh_from_db()
        assert target.name == "Target name"
        assert target.origin == Cluster.Origin.HUMAN

    def test_merging_into_an_already_human_target_stays_human(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "42"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.HUMAN
        )
        client.force_login(member)

        response = client.post(
            self._merge_url(project, source), {"target_cluster_id": target.pk}
        )

        assert response.status_code == 200
        target.refresh_from_db()
        assert target.origin == Cluster.Origin.HUMAN

    def test_merging_a_cluster_into_itself_is_a_no_op(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "43"
        )
        cycle = self._make_revealed_cycle(project)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Solo", origin=Cluster.Origin.SUGGESTED
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="Stays put", author=member, cluster=cluster
        )
        client.force_login(member)

        response = client.post(
            self._merge_url(project, cluster), {"target_cluster_id": cluster.pk}
        )

        assert response.status_code == 200
        assert Cluster.objects.filter(pk=cluster.pk).exists()
        card.refresh_from_db()
        assert card.cluster_id == cluster.pk

    def test_missing_target_is_a_no_op(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "44"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._merge_url(project, source), {})

        assert response.status_code == 200
        assert Cluster.objects.filter(pk=source.pk).exists()

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "45"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_mg", password=VALID_PASSWORD
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(outsider)

        response = client.post(
            self._merge_url(project, source), {"target_cluster_id": target.pk}
        )

        assert response.status_code == 404

    def test_merge_before_reveal_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "46"
        )
        cycle = self._make_collecting_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        target = Cluster.objects.create(
            cycle=cycle, name="Target", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(
            self._merge_url(project, source), {"target_cluster_id": target.pk}
        )

        assert response.status_code == 404


class TestSplitCluster(TestBoardClusterBase):
    def test_splitting_leaves_moved_cards_in_a_new_cluster_and_the_rest_in_the_original(
        self, client, django_user_model
    ):
        # The acceptance criterion this issue explicitly calls out.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "50"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Everything", origin=Cluster.Origin.SUGGESTED
        )
        card1 = Card.objects.create(
            cycle=cycle, category="start", text="C1", author=member, cluster=source
        )
        card2 = Card.objects.create(
            cycle=cycle, category="stop", text="C2", author=member, cluster=source
        )
        card3 = Card.objects.create(
            cycle=cycle, category="continue", text="C3", author=member, cluster=source
        )
        client.force_login(member)

        response = client.post(
            self._split_url(project, source),
            {"card_ids": [card1.pk, card2.pk], "name": "New group"},
        )

        assert response.status_code == 200
        source.refresh_from_db()
        assert set(source.cards.all()) == {card3}
        # Source's own origin is untouched by a split.
        assert source.origin == Cluster.Origin.SUGGESTED

        new_cluster = Cluster.objects.exclude(pk=source.pk).get(cycle=cycle)
        assert new_cluster.name == "New group"
        assert new_cluster.origin == Cluster.Origin.HUMAN
        assert set(new_cluster.cards.all()) == {card1, card2}

    def test_split_works_the_same_on_a_human_origin_cluster(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "51"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Human group", origin=Cluster.Origin.HUMAN
        )
        card1 = Card.objects.create(
            cycle=cycle, category="start", text="H1", author=member, cluster=source
        )
        card2 = Card.objects.create(
            cycle=cycle, category="stop", text="H2", author=member, cluster=source
        )
        client.force_login(member)

        response = client.post(
            self._split_url(project, source), {"card_ids": [card1.pk], "name": "Split off"}
        )

        assert response.status_code == 200
        card1.refresh_from_db()
        card2.refresh_from_db()
        assert card1.cluster.name == "Split off"
        assert card1.cluster.origin == Cluster.Origin.HUMAN
        assert card2.cluster_id == source.pk

    def test_blank_name_falls_back_to_a_default_derived_from_the_source(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "52"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Original", origin=Cluster.Origin.SUGGESTED
        )
        card1 = Card.objects.create(
            cycle=cycle, category="start", text="C1", author=member, cluster=source
        )
        client.force_login(member)

        client.post(self._split_url(project, source), {"card_ids": [card1.pk]})

        card1.refresh_from_db()
        assert card1.cluster.name == "Original (split)"

    def test_card_ids_not_in_this_cluster_are_ignored(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "53"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        elsewhere_card = Card.objects.create(
            cycle=cycle, category="start", text="Not in source", author=member
        )
        client.force_login(member)

        response = client.post(
            self._split_url(project, source), {"card_ids": [elsewhere_card.pk]}
        )

        assert response.status_code == 200
        elsewhere_card.refresh_from_db()
        assert elsewhere_card.cluster_id is None
        # No card actually moved, so no new cluster was created.
        assert Cluster.objects.filter(cycle=cycle).count() == 1

    def test_no_card_ids_is_a_no_op(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "54"
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._split_url(project, source), {})

        assert response.status_code == 200
        assert Cluster.objects.filter(cycle=cycle).count() == 1

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "55"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_sp", password=VALID_PASSWORD
        )
        cycle = self._make_revealed_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        card = Card.objects.create(
            cycle=cycle, category="start", text="C1", author=member, cluster=source
        )
        client.force_login(outsider)

        response = client.post(
            self._split_url(project, source), {"card_ids": [card.pk]}
        )

        assert response.status_code == 404

    def test_split_before_reveal_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "56"
        )
        cycle = self._make_collecting_cycle(project)
        source = Cluster.objects.create(
            cycle=cycle, name="Source", origin=Cluster.Origin.SUGGESTED
        )
        client.force_login(member)

        response = client.post(self._split_url(project, source), {"card_ids": []})

        assert response.status_code == 404
