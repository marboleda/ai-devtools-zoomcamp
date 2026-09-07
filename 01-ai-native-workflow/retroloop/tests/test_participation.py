"""Tests for #11: tracking submission participation without exposing card
content — CycleParticipation rows created on card save, and the "N of M
submitted" indicator on the project page.
"""
import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from projects.models import Card, CycleParticipation, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class TestCycleParticipationCreation:
    def _make_member_and_project(self, django_user_model, username="member", role=None):
        user = django_user_model.objects.create_user(
            username=username, password=VALID_PASSWORD
        )
        owner = django_user_model.objects.create_user(
            username=f"{username}_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Participation Project", created_by=owner)
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

    def test_submitting_a_card_creates_a_participation_row(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "First card", "anonymous": ""},
        )

        participation = CycleParticipation.objects.get(cycle=cycle, member=member)
        assert participation.submitted_at is not None

    def test_a_second_card_from_the_same_member_does_not_create_a_second_row(
        self, client, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)
        url = reverse("create_card", kwargs={"pk": project.pk})

        client.post(url, {"category": "start", "text": "First", "anonymous": ""})
        client.post(url, {"category": "stop", "text": "Second", "anonymous": ""})
        client.post(url, {"category": "continue", "text": "Third", "anonymous": ""})

        assert Card.objects.filter(cycle=cycle, author=member).count() == 3
        assert CycleParticipation.objects.filter(cycle=cycle, member=member).count() == 1

    def test_anonymous_submission_still_creates_a_participation_row_for_the_member(
        self, client, django_user_model
    ):
        # Anonymity affects Card.author, not who is credited with
        # participating — the submitter is always request.user.
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        client.force_login(member)

        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Anon card", "anonymous": "on"},
        )

        card = Card.objects.get(cycle=cycle)
        assert card.author is None
        assert CycleParticipation.objects.filter(cycle=cycle, member=member).exists()

    def test_different_members_each_get_their_own_row(self, client, django_user_model):
        member, project = self._make_member_and_project(django_user_model, username="m1")
        other = django_user_model.objects.create_user(
            username="m2", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=other, role=Membership.Role.MEMBER)
        cycle = self._make_collecting_cycle(project)
        url = reverse("create_card", kwargs={"pk": project.pk})

        client.force_login(member)
        client.post(url, {"category": "start", "text": "From member", "anonymous": ""})
        client.force_login(other)
        client.post(url, {"category": "start", "text": "From other", "anonymous": ""})

        assert CycleParticipation.objects.filter(cycle=cycle).count() == 2
        assert CycleParticipation.objects.filter(cycle=cycle, member=member).exists()
        assert CycleParticipation.objects.filter(cycle=cycle, member=other).exists()

    def test_participation_holds_no_card_data(self, django_user_model):
        # Guards the constraint directly: CycleParticipation exposes no
        # attribute that could carry card text/category/content.
        field_names = {f.name for f in CycleParticipation._meta.get_fields()}
        assert field_names == {"id", "cycle", "member", "submitted_at"}

    def test_db_constraint_prevents_two_rows_for_the_same_member_and_cycle(
        self, django_user_model
    ):
        # Exercised directly at the ORM/DB layer, bypassing the view's
        # get_or_create, the same pattern used for the active-cycle
        # constraint in TestCreateCycle.
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        CycleParticipation.objects.create(cycle=cycle, member=member)

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                CycleParticipation.objects.create(cycle=cycle, member=member)

        assert CycleParticipation.objects.filter(cycle=cycle, member=member).count() == 1

    def test_participation_in_one_cycle_does_not_count_toward_a_sibling_cycle(
        self, django_user_model
    ):
        member, project = self._make_member_and_project(django_user_model)
        cycle = self._make_collecting_cycle(project)
        other_cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 0", state=FeedbackCycle.State.CLOSED
        )
        CycleParticipation.objects.create(cycle=other_cycle, member=member)

        assert CycleParticipation.objects.filter(cycle=cycle).count() == 0
        # The same member can still participate separately in the other cycle.
        CycleParticipation.objects.create(cycle=cycle, member=member)
        assert CycleParticipation.objects.filter(cycle=cycle).count() == 1


@pytest.mark.django_db
class TestParticipationIndicatorOnProjectPage:
    def _make_project_with_members(self, django_user_model, member_count=3):
        owner = django_user_model.objects.create_user(
            username="indicator_owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Indicator Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        members = [owner]
        for i in range(member_count - 1):
            user = django_user_model.objects.create_user(
                username=f"indicator_member_{i}", password=VALID_PASSWORD
            )
            Membership.objects.create(project=project, user=user, role=Membership.Role.MEMBER)
            members.append(user)
        return owner, project, members

    def test_shows_n_of_m_submitted_reflecting_participation_and_member_count(
        self, client, django_user_model
    ):
        owner, project, members = self._make_project_with_members(
            django_user_model, member_count=3
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(members[0])
        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Owner card", "anonymous": ""},
        )
        client.force_login(members[1])
        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {"category": "start", "text": "Member card", "anonymous": ""},
        )

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "2 of 3 submitted" in content

    def test_multiple_cards_from_one_member_still_count_as_one_submission(
        self, client, django_user_model
    ):
        owner, project, members = self._make_project_with_members(
            django_user_model, member_count=2
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(members[0])
        url = reverse("create_card", kwargs={"pk": project.pk})
        client.post(url, {"category": "start", "text": "One", "anonymous": ""})
        client.post(url, {"category": "stop", "text": "Two", "anonymous": ""})
        client.post(url, {"category": "continue", "text": "Three", "anonymous": ""})

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        # 1 (not 3) of 2 submitted: the indicator counts members, not cards.
        content = response.content.decode()
        assert "1 of 2 submitted" in content

    def test_indicator_never_shows_card_text_category_or_count(
        self, client, django_user_model
    ):
        owner, project, members = self._make_project_with_members(
            django_user_model, member_count=2
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(members[0])
        client.post(
            reverse("create_card", kwargs={"pk": project.pk}),
            {
                "category": "start",
                "text": "This exact text must never leak to the project page",
                "anonymous": "",
            },
        )

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "This exact text must never leak to the project page" not in content

    def test_zero_of_m_before_anyone_submits(self, client, django_user_model):
        owner, project, members = self._make_project_with_members(
            django_user_model, member_count=4
        )
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(owner)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "0 of 4 submitted" in content

    def test_no_indicator_shown_when_there_is_no_active_cycle(
        self, client, django_user_model
    ):
        owner, project, members = self._make_project_with_members(
            django_user_model, member_count=2
        )
        client.force_login(owner)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert "submitted" not in content.lower()
