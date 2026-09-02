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
        content = response.content.decode()
        assert "Ship it" not in content

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
