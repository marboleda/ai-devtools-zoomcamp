"""Tests for card submission (#8): a project member adding Start / Stop /
Continue cards to the active feedback cycle while it is in "collecting".
"""
import pytest
from django.urls import reverse

from projects.models import Card, FeedbackCycle, Membership, Project
from projects.views import ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestCreateCard:
    def _make_member_and_project(self, django_user_model, username="member", role=None):
        user = django_user_model.objects.create_user(
            username=username, password=VALID_PASSWORD
        )
        owner = django_user_model.objects.create_user(
            username=f"{username}_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Card Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        Membership.objects.create(
            project=project,
            user=user,
            role=role or Membership.Role.MEMBER,
        )
        return user, project

    def _make_collecting_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

    # -- Any member can submit, in any of the three categories --

    def test_facilitator_can_submit_a_card(self, client, django_user_model):
        owner = django_user_model.objects.create_user(
            username="fac", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Fac Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        cycle = self._make_collecting_cycle(project)
        client.force_login(owner)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Ship faster", "anonymous": ""},
        )

        assert response.status_code == 200
        card = Card.objects.get(cycle=cycle)
        assert card.category == "start"
        assert card.author == owner

    @pytest.mark.parametrize("category", ["start", "stop", "continue"])
    def test_member_can_submit_in_each_category(
        self, client, django_user_model, category
    ):
        member, project = self._make_member_and_project(
            django_user_model, username=f"cat_{category}"
        )
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": category, "text": "Some feedback", "anonymous": ""},
        )

        assert response.status_code == 200
        card = Card.objects.get(cycle=cycle)
        assert card.category == category

    def test_member_can_submit_multiple_cards_in_the_same_category(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)
        url = reverse("create_card", kwargs={"pk": project.pk})

        client.post(url, {"category": "start", "text": "First", "anonymous": ""})
        client.post(url, {"category": "start", "text": "Second", "anonymous": ""})
        client.post(url, {"category": "start", "text": "Third", "anonymous": ""})

        assert Card.objects.filter(cycle=cycle, category="start").count() == 3

    def test_invalid_category_is_rejected_with_validation_error(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "sideways", "text": "Not a real category", "anonymous": ""},
        )

        assert response.status_code == 200
        assert not Card.objects.exists()

    # -- Text validation --

    def test_text_over_280_characters_is_rejected_not_truncated(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "x" * 281, "anonymous": ""},
        )

        assert response.status_code == 200
        assert not Card.objects.exists()

    def test_exactly_280_characters_is_accepted(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "x" * 280, "anonymous": ""},
        )

        assert response.status_code == 200
        assert Card.objects.get(cycle=cycle).text == "x" * 280

    def test_blank_text_is_rejected_and_creates_no_card(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "", "anonymous": ""},
        )

        assert response.status_code == 200
        assert not Card.objects.exists()

    def test_whitespace_only_text_is_rejected_and_creates_no_card(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "     ", "anonymous": ""},
        )

        assert response.status_code == 200
        assert not Card.objects.exists()

    # -- Anonymity / edit token --

    def test_anonymous_card_has_no_author_and_a_hashed_edit_token(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "stop", "text": "Anon feedback", "anonymous": "on"},
        )

        card = Card.objects.get(cycle=cycle)
        assert card.author is None
        assert card.edit_token_hash
        # The plaintext token is never in the response body.
        assert card.edit_token_hash not in response.content.decode()

    def test_attributed_card_has_author_set_and_no_edit_token_hash(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "stop", "text": "Attributed feedback", "anonymous": ""},
        )

        card = Card.objects.get(cycle=cycle)
        assert card.author == member
        assert card.edit_token_hash is None

    def test_anonymous_submission_stores_plaintext_token_only_in_session(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "continue", "text": "Keep doing this", "anonymous": "on"},
        )

        card = Card.objects.get(cycle=cycle)
        tokens = client.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY]
        plaintext_token = tokens[str(card.pk)]

        assert plaintext_token
        # Only a hash is ever persisted on the card row.
        assert plaintext_token != card.edit_token_hash

        from projects.models import hash_edit_token

        assert hash_edit_token(plaintext_token) == card.edit_token_hash

    # -- Cycle state gating --

    def test_submitting_while_cycle_is_collecting_succeeds(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Fine", "anonymous": ""},
        )

        assert Card.objects.count() == 1
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "state",
        [
            FeedbackCycle.State.REVEALED,
            FeedbackCycle.State.CLUSTERING,
            FeedbackCycle.State.VOTING,
            FeedbackCycle.State.DISCUSSING,
        ],
    )
    def test_submitting_once_cycle_is_no_longer_collecting_is_rejected(
        self, client, django_user_model, state
    ):
        member, project = self._make_member_and_project(
            django_user_model, username=f"late_{state}"
        )
        FeedbackCycle.objects.create(project=project, week="Cycle 1", state=state)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Too late", "anonymous": ""},
        )

        assert not Card.objects.exists()
        assert response.status_code == 200

    def test_no_active_cycle_makes_the_route_unreachable(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        client.force_login(member)

        response = client.get(reverse("create_card", kwargs={"pk": project.pk}))

        assert response.status_code == 404

    def test_no_active_cycle_makes_post_unreachable_too(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "No cycle", "anonymous": ""},
        )

        assert response.status_code == 404
        assert not Card.objects.exists()

    def test_closed_cycle_is_treated_as_no_active_cycle(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.CLOSED
        )
        client.force_login(member)

        response = client.get(reverse("create_card", kwargs={"pk": project.pk}))

        assert response.status_code == 404

    # -- Membership / access control --

    def test_non_member_of_existing_project_gets_404(self, client, django_user_model):
        owner = django_user_model.objects.create_user(
            username="owner_x", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Not Yours", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        self._make_collecting_cycle(project)
        outsider = django_user_model.objects.create_user(
            username="outsider_x", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        get_response = client.get(reverse("create_card", kwargs={"pk": project.pk}))
        post_response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Sneaky", "anonymous": ""},
        )

        assert get_response.status_code == 404
        assert post_response.status_code == 404
        assert not Card.objects.exists()

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_user", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("create_card", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        owner = django_user_model.objects.create_user(
            username="owner_y", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Login Required", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        self._make_collecting_cycle(project)

        get_response = client.get(reverse("create_card", kwargs={"pk": project.pk}))
        post_response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Sneaky", "anonymous": ""},
        )

        assert get_response.status_code == 302
        assert get_response.url.startswith(reverse("login"))
        assert post_response.status_code == 302
        assert post_response.url.startswith(reverse("login"))
        assert not Card.objects.exists()

    # -- Post-submission redirect / UX --

    def test_successful_submission_returns_to_a_cleared_form_not_project_detail(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)

        response = client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Ship it", "anonymous": ""},
        )

        assert response.status_code == 200
        assert response.request["PATH_INFO"] == reverse(
            "create_card", kwargs={"pk": project.pk}
        )
        # The *form* is cleared for the next submission — "Ship it" is not
        # echoed back into a textarea/input value. It's expected to appear
        # once now, though, in the "your cards" list (#9) the same page
        # shows for a just-submitted card.
        content = response.content.decode()
        assert content.count("Ship it") == 1
        assert 'value="Ship it"' not in content
        assert ">Ship it</textarea>" not in content

    # -- Project detail page link visibility --

    def test_project_detail_shows_add_card_link_only_while_collecting(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)
        card_url = reverse("create_card", kwargs={"pk": project.pk})

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))
        assert card_url in response.content.decode()

        cycle.state = FeedbackCycle.State.REVEALED
        cycle.save()

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))
        assert card_url not in response.content.decode()

    def test_project_detail_hides_add_card_link_when_no_active_cycle(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        client.force_login(member)
        card_url = reverse("create_card", kwargs={"pk": project.pk})

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        assert card_url not in response.content.decode()


@pytest.mark.django_db
class TestEditAndWithdrawCard:
    """Tests for #9: editing or withdrawing your own card before reveal."""

    def _make_member_and_project(self, django_user_model, username="member", role=None):
        user = django_user_model.objects.create_user(
            username=username, password=VALID_PASSWORD
        )
        owner = django_user_model.objects.create_user(
            username=f"{username}_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Edit Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        Membership.objects.create(
            project=project, user=user, role=role or Membership.Role.MEMBER
        )
        return user, project

    def _make_collecting_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

    def _edit_url(self, project, card):
        return reverse("edit_card", kwargs={"pk": project.pk, "card_id": card.pk})

    def _withdraw_url(self, project, card):
        return reverse("withdraw_card", kwargs={"pk": project.pk, "card_id": card.pk})

    def _submit_anonymous_card(self, client, project, category="start", text="Anon text"):
        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": category, "text": text, "anonymous": "on"},
        )
        card = Card.objects.get(cycle__project=project, text=text)
        token = client.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY][str(card.pk)]
        return card, token

    # -- Attributed cards: own vs. someone else's --

    def test_author_can_edit_their_own_attributed_card(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Original", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "stop", "text": "Updated"}
        )

        assert response.status_code == 302
        card.refresh_from_db()
        assert card.category == "stop"
        assert card.text == "Updated"
        assert card.author == member

    def test_author_can_withdraw_their_own_attributed_card(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Withdraw me", author=member
        )
        client.force_login(member)

        response = client.post(self._withdraw_url(project, card))

        assert response.status_code == 302
        assert not Card.objects.filter(pk=card.pk).exists()

    def test_editing_another_members_card_gets_404(self, client, django_user_model):
        owner_member, project = self._make_member_and_project(
            django_user_model, username="owner_of_card"
        )
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Not yours", author=owner_member
        )
        other = django_user_model.objects.create_user(
            username="other_member", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=other, role=Membership.Role.MEMBER
        )
        client.force_login(other)

        get_response = client.get(self._edit_url(project, card))
        post_response = client.post(
            self._edit_url(project, card), {"category": "stop", "text": "Hijacked"}
        )

        assert get_response.status_code == 404
        assert post_response.status_code == 404
        card.refresh_from_db()
        assert card.text == "Not yours"

    def test_withdrawing_another_members_card_gets_404(self, client, django_user_model):
        owner_member, project = self._make_member_and_project(
            django_user_model, username="owner_of_card2"
        )
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Not yours either", author=owner_member
        )
        other = django_user_model.objects.create_user(
            username="other_member2", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=other, role=Membership.Role.MEMBER
        )
        client.force_login(other)

        response = client.post(self._withdraw_url(project, card))

        assert response.status_code == 404
        assert Card.objects.filter(pk=card.pk).exists()

    # -- Anonymous cards: correct token vs. no token --

    def test_anonymous_card_can_be_edited_with_correct_session_token(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Anon original")

        response = client.post(
            self._edit_url(project, card), {"category": "continue", "text": "Anon updated"}
        )

        assert response.status_code == 302
        card.refresh_from_db()
        assert card.category == "continue"
        assert card.text == "Anon updated"
        # Still anonymous: attribution didn't change as a side effect of editing.
        assert card.author is None
        assert card.edit_token_hash

    def test_anonymous_card_can_be_withdrawn_with_correct_session_token(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Anon withdraw")

        response = client.post(self._withdraw_url(project, card))

        assert response.status_code == 302
        assert not Card.objects.filter(pk=card.pk).exists()

    def test_anonymous_card_edit_without_matching_token_gets_404(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Anon secret")

        # A fresh session (e.g. a different browser/device) has no token
        # for this card at all.
        from django.test import Client

        other_client = Client()
        other_client.force_login(member)

        response = other_client.post(
            self._edit_url(project, card), {"category": "stop", "text": "Stolen"}
        )

        assert response.status_code == 404
        card.refresh_from_db()
        assert card.text == "Anon secret"

    def test_anonymous_card_withdraw_without_matching_token_gets_404(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Anon secret 2")

        from django.test import Client

        other_client = Client()
        other_client.force_login(member)

        response = other_client.post(self._withdraw_url(project, card))

        assert response.status_code == 404
        assert Card.objects.filter(pk=card.pk).exists()

    def test_attributed_member_without_the_right_card_id_cannot_reach_an_anonymous_card(
        self, client, django_user_model
    ):
        # An attributed member (correctly logged in, correctly a project
        # member) still can't touch an anonymous card that isn't theirs —
        # the session token is what gates it, not membership.
        anon_submitter, project = self._make_member_and_project(
            django_user_model, username="anon_submitter"
        )
        self._make_collecting_cycle(project)
        client.force_login(anon_submitter)
        card, _token = self._submit_anonymous_card(client, project, text="Truly anon")

        other = django_user_model.objects.create_user(
            username="curious_member", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)
        client.force_login(other)

        response = client.post(self._withdraw_url(project, card))

        assert response.status_code == 404
        assert Card.objects.filter(pk=card.pk).exists()

    # -- Validation on edit --

    def test_edit_rejects_invalid_category_and_leaves_card_unchanged(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Keep me", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "sideways", "text": "Bad"}
        )

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.category == "start"
        assert card.text == "Keep me"

    def test_edit_rejects_blank_text_and_leaves_card_unchanged(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Keep me too", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "start", "text": ""}
        )

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.text == "Keep me too"

    def test_edit_rejects_text_over_280_characters_and_leaves_card_unchanged(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Original short text", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "start", "text": "x" * 281}
        )

        assert response.status_code == 200
        card.refresh_from_db()
        assert card.text == "Original short text"

    def test_edit_accepts_exactly_280_characters(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="short", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "start", "text": "x" * 280}
        )

        assert response.status_code == 302
        card.refresh_from_db()
        assert card.text == "x" * 280

    def test_edit_form_has_no_anonymous_checkbox(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Attributed", author=member
        )
        client.force_login(member)

        response = client.get(self._edit_url(project, card))

        assert response.status_code == 200
        content = response.content.decode().lower()
        assert "anonymous" not in content

    # -- Attribution never changes on edit --

    def test_editing_an_attributed_card_keeps_it_attributed(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Attributed", author=member
        )
        client.force_login(member)

        client.post(self._edit_url(project, card), {"category": "stop", "text": "Still me"})

        card.refresh_from_db()
        assert card.author == member
        assert card.edit_token_hash is None

    def test_editing_an_anonymous_card_keeps_it_anonymous(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Anon stays")
        original_hash = card.edit_token_hash

        client.post(
            self._edit_url(project, card), {"category": "stop", "text": "Anon still"}
        )

        card.refresh_from_db()
        assert card.author is None
        assert card.edit_token_hash == original_hash

    # -- Withdraw is a hard delete, POST-only --

    def test_withdraw_removes_the_row_from_the_database(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Gone soon", author=member
        )
        card_id = card.pk
        client.force_login(member)

        client.post(self._withdraw_url(project, card))

        assert not Card.objects.filter(pk=card_id).exists()

    def test_withdrawing_an_anonymous_card_removes_its_session_token_entry(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        self._make_collecting_cycle(project)
        client.force_login(member)
        card, _token = self._submit_anonymous_card(client, project, text="Token cleanup")
        card_id = card.pk
        assert str(card_id) in client.session[ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY]

        client.post(self._withdraw_url(project, card))

        assert not Card.objects.filter(pk=card_id).exists()
        assert str(card_id) not in client.session.get(
            ANONYMOUS_CARD_EDIT_TOKENS_SESSION_KEY, {}
        )

    def test_withdraw_get_request_does_not_delete_the_card(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Still here", author=member
        )
        client.force_login(member)

        response = client.get(self._withdraw_url(project, card))

        assert response.status_code == 405
        assert Card.objects.filter(pk=card.pk).exists()

    # -- Cycle state gating: rejected once no longer "collecting" --

    @pytest.mark.parametrize(
        "state",
        [
            FeedbackCycle.State.REVEALED,
            FeedbackCycle.State.CLUSTERING,
            FeedbackCycle.State.VOTING,
            FeedbackCycle.State.DISCUSSING,
            FeedbackCycle.State.CLOSED,
        ],
    )
    def test_editing_is_rejected_once_cycle_is_no_longer_collecting_even_for_owner(
        self, client, django_user_model, state
    ):
        member, project = self._make_member_and_project(
            django_user_model, username=f"edit_late_{state}"
        )
        cycle = FeedbackCycle.objects.create(project=project, week="Cycle 1", state=state)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Frozen", author=member
        )
        client.force_login(member)

        response = client.post(
            self._edit_url(project, card), {"category": "stop", "text": "Too late"}
        )

        assert response.status_code == 302
        card.refresh_from_db()
        assert card.category == "start"
        assert card.text == "Frozen"

    @pytest.mark.parametrize(
        "state",
        [
            FeedbackCycle.State.REVEALED,
            FeedbackCycle.State.CLUSTERING,
            FeedbackCycle.State.VOTING,
            FeedbackCycle.State.DISCUSSING,
            FeedbackCycle.State.CLOSED,
        ],
    )
    def test_withdrawing_is_rejected_once_cycle_is_no_longer_collecting_even_for_owner(
        self, client, django_user_model, state
    ):
        member, project = self._make_member_and_project(
            django_user_model, username=f"withdraw_late_{state}"
        )
        cycle = FeedbackCycle.objects.create(project=project, week="Cycle 1", state=state)
        card = Card.objects.create(
            cycle=cycle, category="start", text="Frozen too", author=member
        )
        client.force_login(member)

        response = client.post(self._withdraw_url(project, card))

        assert response.status_code == 302
        assert Card.objects.filter(pk=card.pk).exists()

    # -- The "your cards" list on the card submission page --

    def test_card_submission_page_lists_own_attributed_and_anonymous_cards(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)
        attributed = Card.objects.create(
            cycle=cycle, category="start", text="My attributed card", author=member
        )
        anon_card, _token = self._submit_anonymous_card(
            client, project, text="My anon card"
        )

        response = client.get(reverse("create_card", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "My attributed card" in content
        assert "My anon card" in content
        assert self._edit_url(project, attributed) in content
        assert self._withdraw_url(project, attributed) in content
        assert self._edit_url(project, anon_card) in content
        assert self._withdraw_url(project, anon_card) in content

    def test_card_submission_page_does_not_list_other_members_cards(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        other = django_user_model.objects.create_user(
            username="not_visible", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)
        Card.objects.create(
            cycle=cycle, category="start", text="Someone elses card", author=other
        )
        client.force_login(member)

        response = client.get(reverse("create_card", kwargs={"pk": project.pk}))

        assert "Someone elses card" not in response.content.decode()
