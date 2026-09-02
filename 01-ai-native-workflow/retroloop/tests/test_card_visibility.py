"""Tests for #10: Card.objects.visible_to, the single reusable manager
method that enforces pre-reveal card privacy (stack.md invariant #3).
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import Card, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestCardVisibleTo:
    def _make_member_and_project(self, django_user_model, username="member", role=None):
        user = django_user_model.objects.create_user(
            username=username, password=VALID_PASSWORD
        )
        owner = django_user_model.objects.create_user(
            username=f"{username}_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Visibility Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        Membership.objects.create(
            project=project, user=user, role=role or Membership.Role.MEMBER
        )
        return user, project

    def _make_cycle(self, project, revealed=False):
        return FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.REVEALED if revealed else FeedbackCycle.State.COLLECTING,
            revealed_at=timezone.now() if revealed else None,
        )

    # -- Pre-reveal: only the viewer's own cards --

    def test_pre_reveal_member_a_never_sees_member_bs_attributed_or_anonymous_card(
        self, django_user_model
    ):
        member_a, project = self._make_member_and_project(django_user_model, username="a")
        member_b = django_user_model.objects.create_user(
            username="b", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member_b, role=Membership.Role.MEMBER)
        cycle = self._make_cycle(project)

        b_attributed = Card.objects.create(
            cycle=cycle, category="start", text="B attributed", author=member_b
        )
        b_anonymous = Card.objects.create(
            cycle=cycle, category="stop", text="B anonymous", author=None,
            edit_token_hash="deadbeef",
        )
        a_attributed = Card.objects.create(
            cycle=cycle, category="continue", text="A attributed", author=member_a
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member_a)

        assert set(visible) == {a_attributed}
        assert b_attributed not in visible
        assert b_anonymous not in visible

    def test_pre_reveal_viewer_sees_own_attributed_and_own_anonymous_cards(
        self, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_cycle(project)

        own_attributed = Card.objects.create(
            cycle=cycle, category="start", text="Mine attributed", author=member
        )
        own_anonymous = Card.objects.create(
            cycle=cycle, category="stop", text="Mine anonymous", author=None,
            edit_token_hash="abc123",
        )
        someone_elses_anonymous = Card.objects.create(
            cycle=cycle, category="continue", text="Not mine anonymous", author=None,
            edit_token_hash="fff000",
        )

        visible = Card.objects.visible_to(
            cycle=cycle, viewer=member, anonymous_card_ids=[own_anonymous.pk]
        )

        assert set(visible) == {own_attributed, own_anonymous}
        assert someone_elses_anonymous not in visible

    # -- Facilitator gets no special treatment pre-reveal --

    def test_pre_reveal_facilitator_is_scoped_the_same_as_any_other_member(
        self, django_user_model
    ):
        facilitator = django_user_model.objects.create_user(
            username="fac", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Fac Visibility", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username="regular", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        cycle = self._make_cycle(project)

        facilitator_card = Card.objects.create(
            cycle=cycle, category="start", text="Facilitator's own", author=facilitator
        )
        member_attributed = Card.objects.create(
            cycle=cycle, category="start", text="Member attributed", author=member
        )
        member_anonymous = Card.objects.create(
            cycle=cycle, category="stop", text="Member anonymous", author=None,
            edit_token_hash="tok",
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=facilitator)

        assert set(visible) == {facilitator_card}
        assert member_attributed not in visible
        assert member_anonymous not in visible

    # -- Post-reveal: everything --

    def test_post_reveal_any_viewer_sees_every_card_in_the_cycle(self, django_user_model):
        member_a, project = self._make_member_and_project(django_user_model, username="a2")
        member_b = django_user_model.objects.create_user(
            username="b2", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member_b, role=Membership.Role.MEMBER)
        cycle = self._make_cycle(project, revealed=True)

        a_card = Card.objects.create(
            cycle=cycle, category="start", text="A's card", author=member_a
        )
        b_card = Card.objects.create(
            cycle=cycle, category="start", text="B's card", author=member_b
        )
        b_anonymous = Card.objects.create(
            cycle=cycle, category="stop", text="B's anon card", author=None,
            edit_token_hash="tok2",
        )

        # No anonymous_card_ids passed at all.
        visible = Card.objects.visible_to(cycle=cycle, viewer=member_a)

        assert set(visible) == {a_card, b_card, b_anonymous}

    def test_post_reveal_visibility_is_unaffected_by_anonymous_card_ids_argument(
        self, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model, username="a3")
        cycle = self._make_cycle(project, revealed=True)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Revealed card", author=None,
            edit_token_hash="whatever",
        )

        # Whether or not the caller passes an (irrelevant, post-reveal)
        # anonymous-ID collection, everything in the cycle is returned.
        visible_without = Card.objects.visible_to(cycle=cycle, viewer=member)
        visible_with = Card.objects.visible_to(
            cycle=cycle, viewer=member, anonymous_card_ids=[999999]
        )

        assert set(visible_without) == {card}
        assert set(visible_with) == {card}

    # -- Cycle scoping --

    def test_card_in_sibling_cycle_is_never_returned_pre_reveal(self, django_user_model):
        member, project = self._make_member_and_project(django_user_model, username="a4")
        cycle = self._make_cycle(project)
        other_cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 0", state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
        )
        sibling_card = Card.objects.create(
            cycle=other_cycle, category="start", text="Sibling cycle card", author=member
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member)

        assert sibling_card not in visible

    def test_card_in_sibling_cycle_is_never_returned_post_reveal(self, django_user_model):
        member, project = self._make_member_and_project(django_user_model, username="a5")
        cycle = self._make_cycle(project, revealed=True)
        other_cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 0", state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
        )
        sibling_card = Card.objects.create(
            cycle=other_cycle, category="start", text="Sibling cycle card 2", author=member
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member)

        assert sibling_card not in visible

    # -- Empty/omitted anonymous-ID collection never widens the result --

    def test_omitted_anonymous_card_ids_does_not_leak_other_members_anonymous_cards(
        self, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model, username="a6")
        cycle = self._make_cycle(project)
        Card.objects.create(
            cycle=cycle, category="start", text="Someone's anon card", author=None,
            edit_token_hash="tok3",
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member)

        assert list(visible) == []

    def test_empty_list_anonymous_card_ids_does_not_leak_other_members_anonymous_cards(
        self, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model, username="a7")
        cycle = self._make_cycle(project)
        Card.objects.create(
            cycle=cycle, category="start", text="Someone's anon card 2", author=None,
            edit_token_hash="tok4",
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member, anonymous_card_ids=[])

        assert list(visible) == []

    def test_none_anonymous_card_ids_does_not_raise_or_leak(self, django_user_model):
        # Guards specifically against a pk__in=None-style bug silently
        # widening the result to everyone's anonymous cards.
        member, project = self._make_member_and_project(django_user_model, username="a8")
        cycle = self._make_cycle(project)
        Card.objects.create(
            cycle=cycle, category="start", text="Someone's anon card 3", author=None,
            edit_token_hash="tok5",
        )

        visible = Card.objects.visible_to(cycle=cycle, viewer=member, anonymous_card_ids=None)

        assert list(visible) == []

    # -- Ordering --

    def test_results_are_ordered_by_created_at(self, django_user_model):
        member, project = self._make_member_and_project(django_user_model, username="a9")
        cycle = self._make_cycle(project, revealed=True)
        first = Card.objects.create(cycle=cycle, category="start", text="First", author=member)
        second = Card.objects.create(cycle=cycle, category="start", text="Second", author=member)
        third = Card.objects.create(cycle=cycle, category="start", text="Third", author=member)

        visible = list(Card.objects.visible_to(cycle=cycle, viewer=member))

        assert visible == [first, second, third]


@pytest.mark.django_db
class TestOwnCardsForCycleRefactor:
    """#10 requires _own_cards_for_cycle (used by create_card's "your
    cards" list) to be refactored onto Card.objects.visible_to without
    changing its visible behaviour.
    """

    def _make_member_and_project(self, django_user_model, username="member", role=None):
        user = django_user_model.objects.create_user(
            username=username, password=VALID_PASSWORD
        )
        owner = django_user_model.objects.create_user(
            username=f"{username}_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Refactor Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        Membership.objects.create(
            project=project, user=user, role=role or Membership.Role.MEMBER
        )
        return user, project

    def test_create_card_page_still_shows_exactly_the_viewers_own_cards(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        other = django_user_model.objects.create_user(
            username="other_refactor", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)

        own_card = Card.objects.create(
            cycle=cycle, category="start", text="My own card", author=member
        )
        Card.objects.create(
            cycle=cycle, category="start", text="Other member's card", author=other
        )

        client.force_login(member)
        response = client.get(reverse("create_card", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "My own card" in content
        assert "Other member's card" not in content
        assert own_card.text in content
