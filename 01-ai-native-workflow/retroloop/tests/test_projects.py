import re
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse

from projects.models import JOIN_CODE_ALPHABET, FeedbackCycle, Membership, Project

JOIN_CODE_RE = re.compile(rf"^[{JOIN_CODE_ALPHABET}]{{8}}$")


@pytest.mark.django_db
class TestCreateProject:
    def test_signed_in_user_creates_project_and_becomes_facilitator(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="alice", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.post(
            reverse("create_project"),
            {"name": "Team Retro", "description": "Weekly retro project"},
        )

        project = Project.objects.get(name="Team Retro")
        assert response.status_code == 302
        assert response.url == reverse("project_detail", kwargs={"pk": project.pk})
        assert project.created_by == user
        assert project.description == "Weekly retro project"

        membership = Membership.objects.get(project=project, user=user)
        assert membership.role == Membership.Role.FACILITATOR

    def test_join_code_is_generated_in_the_expected_format(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="bob", password="correct horse battery staple"
        )
        client.force_login(user)

        client.post(reverse("create_project"), {"name": "Another Project"})

        project = Project.objects.get(name="Another Project")
        assert JOIN_CODE_RE.match(project.join_code)
        for excluded in "0O1IL":
            assert excluded not in project.join_code

    def test_join_code_collision_is_retried_not_failed(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="carol", password="correct horse battery staple"
        )
        client.force_login(user)

        colliding_code = "AAAAAAAA"
        Project.objects.filter(pk=Project.objects.create(
            name="Existing", created_by=user
        ).pk).update(join_code=colliding_code)

        codes = iter([colliding_code, "BBBBBBBB"])

        def fake_generate():
            return next(codes)

        with patch("projects.models.generate_join_code", side_effect=fake_generate):
            response = client.post(reverse("create_project"), {"name": "Retry Project"})

        assert response.status_code == 302
        new_project = Project.objects.get(name="Retry Project")
        assert new_project.join_code == "BBBBBBBB"

    def test_blank_name_shows_validation_error_and_creates_nothing(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="dave", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.post(reverse("create_project"), {"name": "", "description": ""})

        assert response.status_code == 200
        assert not Project.objects.exists()
        assert not Membership.objects.exists()
        assert "field is required" in response.content.decode().lower()

    def test_name_over_100_characters_shows_validation_error_and_creates_nothing(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="erin", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.post(
            reverse("create_project"), {"name": "x" * 101, "description": ""}
        )

        assert response.status_code == 200
        assert not Project.objects.exists()

    def test_description_is_optional(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="frank", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.post(reverse("create_project"), {"name": "No Description"})

        assert response.status_code == 302
        project = Project.objects.get(name="No Description")
        assert project.description == ""

    def test_duplicate_project_names_are_both_allowed(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="grace", password="correct horse battery staple"
        )
        client.force_login(user)

        response1 = client.post(reverse("create_project"), {"name": "Same Name"})
        response2 = client.post(reverse("create_project"), {"name": "Same Name"})

        assert response1.status_code == 302
        assert response2.status_code == 302
        assert Project.objects.filter(name="Same Name").count() == 2

    def test_anonymous_get_redirected_to_login(self, client):
        response = client.get(reverse("create_project"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        assert not Project.objects.exists()

    def test_anonymous_post_redirected_to_login_and_creates_nothing(self, client):
        response = client.post(reverse("create_project"), {"name": "Sneaky Project"})

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        assert not Project.objects.exists()
        assert not Membership.objects.exists()


@pytest.mark.django_db
class TestJoinProject:
    def test_valid_code_creates_membership_and_redirects_to_project_page(
        self, client, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="ivy", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Joinable", created_by=owner)

        joiner = django_user_model.objects.create_user(
            username="jack", password="correct horse battery staple"
        )
        client.force_login(joiner)

        response = client.post(
            reverse("join_project"), {"join_code": project.join_code}
        )

        assert response.status_code == 302
        assert response.url == reverse("project_detail", kwargs={"pk": project.pk})
        membership = Membership.objects.get(project=project, user=joiner)
        assert membership.role == Membership.Role.MEMBER
        assert Membership.objects.filter(project=project, user=joiner).count() == 1

    def test_joining_again_with_same_code_different_case_or_whitespace_is_idempotent(
        self, client, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="kate", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Repeat Join", created_by=owner)

        joiner = django_user_model.objects.create_user(
            username="liam", password="correct horse battery staple"
        )
        client.force_login(joiner)

        response1 = client.post(
            reverse("join_project"), {"join_code": project.join_code}
        )
        response2 = client.post(
            reverse("join_project"),
            {"join_code": f"  {project.join_code.lower()}  "},
        )

        assert response1.status_code == 302
        assert response2.status_code == 200
        assert Membership.objects.filter(project=project, user=joiner).count() == 1
        assert "already a member" in response2.content.decode().lower()

    def test_creator_joining_own_project_gets_already_a_member_message(
        self, client, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="mia", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Own Project", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        client.force_login(owner)

        response = client.post(
            reverse("join_project"), {"join_code": project.join_code}
        )

        assert response.status_code == 200
        assert "already a member" in response.content.decode().lower()
        assert Membership.objects.filter(project=project, user=owner).count() == 1

    def test_invalid_code_shows_error_and_creates_no_membership(
        self, client, django_user_model
    ):
        user = django_user_model.objects.create_user(
            username="noah", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.post(
            reverse("join_project"), {"join_code": "NOTREAL1"}
        )

        assert response.status_code == 200
        assert not Membership.objects.exists()
        assert "invalid" in response.content.decode().lower()

    def test_case_insensitive_and_whitespace_tolerant_lookup_succeeds(
        self, client, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="olivia", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Case Test", created_by=owner)

        joiner = django_user_model.objects.create_user(
            username="paul", password="correct horse battery staple"
        )
        client.force_login(joiner)

        response = client.post(
            reverse("join_project"),
            {"join_code": f"  {project.join_code.lower()}  "},
        )

        assert response.status_code == 302
        assert Membership.objects.filter(project=project, user=joiner).exists()

    def test_anonymous_get_redirected_to_login(self, client):
        response = client.get(reverse("join_project"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_anonymous_post_redirected_to_login_and_creates_no_membership(
        self, client, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="quinn", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Anon Join Attempt", created_by=owner)

        response = client.post(
            reverse("join_project"), {"join_code": project.join_code}
        )

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        assert not Membership.objects.exists()


@pytest.mark.django_db
class TestProjectDetail:
    def test_facilitator_sees_project_name_members_and_join_code(
        self, client, django_user_model
    ):
        facilitator = django_user_model.objects.create_user(
            username="henry", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Confirm Me", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username="iris", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        client.force_login(facilitator)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        assert response.status_code == 200
        content = response.content.decode()
        assert "Confirm Me" in content
        assert project.join_code in content
        assert "henry" in content
        assert "iris" in content

    def test_regular_member_does_not_see_join_code(self, client, django_user_model):
        facilitator = django_user_model.objects.create_user(
            username="jane", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Members Only", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username="kyle", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        client.force_login(member)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        assert response.status_code == 200
        content = response.content.decode()
        assert project.join_code not in content

    def test_members_are_ordered_by_joined_at(self, client, django_user_model):
        facilitator = django_user_model.objects.create_user(
            username="liam", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Ordered", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        second = django_user_model.objects.create_user(
            username="mona", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=second, role=Membership.Role.MEMBER)
        third = django_user_model.objects.create_user(
            username="nate", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=third, role=Membership.Role.MEMBER)
        client.force_login(facilitator)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert content.index("liam") < content.index("mona") < content.index("nate")

    def test_placeholder_sections_are_present(self, client, django_user_model):
        facilitator = django_user_model.objects.create_user(
            username="oscar", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Placeholders", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        client.force_login(facilitator)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode().lower()
        assert "current cycle" in content
        assert "open action items" in content
        assert "previous retrospectives" in content

    def test_non_member_of_existing_project_gets_404(self, client, django_user_model):
        owner = django_user_model.objects.create_user(
            username="petra", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Not Yours", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )
        outsider = django_user_model.objects.create_user(
            username="quinlan", password="correct horse battery staple"
        )
        client.force_login(outsider)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        assert response.status_code == 404

    def test_nonexistent_project_id_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ruth", password="correct horse battery staple"
        )
        client.force_login(user)

        response = client.get(reverse("project_detail", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_anonymous_user_redirected_to_login(self, client, django_user_model):
        owner = django_user_model.objects.create_user(
            username="sam", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Anon View Attempt", created_by=owner)
        Membership.objects.create(
            project=project, user=owner, role=Membership.Role.FACILITATOR
        )

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_facilitator_sees_start_cycle_action_when_none_active(
        self, client, django_user_model
    ):
        facilitator = django_user_model.objects.create_user(
            username="tara", password="correct horse battery staple"
        )
        project = Project.objects.create(name="No Cycle Yet", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        client.force_login(facilitator)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert reverse("create_cycle", kwargs={"pk": project.pk}) in content
        assert "start a new cycle" in content.lower()

    def test_member_sees_neutral_message_and_no_action_when_none_active(
        self, client, django_user_model
    ):
        facilitator = django_user_model.objects.create_user(
            username="uma", password="correct horse battery staple"
        )
        project = Project.objects.create(name="No Cycle For Member", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username="victor", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        client.force_login(member)

        response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))

        content = response.content.decode()
        assert reverse("create_cycle", kwargs={"pk": project.pk}) not in content
        assert "no active cycle yet" in content.lower()

    def test_any_member_sees_cycle_label_and_state_when_one_is_active(
        self, client, django_user_model
    ):
        facilitator = django_user_model.objects.create_user(
            username="wendy", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Has Cycle", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username="xavier", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        create_url = reverse("create_cycle", kwargs={"pk": project.pk})

        for logged_in_as in (facilitator, member):
            client.force_login(logged_in_as)
            response = client.get(reverse("project_detail", kwargs={"pk": project.pk}))
            content = response.content.decode()
            assert "Cycle 1" in content
            assert "collecting" in content.lower()
            assert create_url not in content


@pytest.mark.django_db
class TestCreateCycle:
    def _make_facilitator_and_project(self, django_user_model, username="fac"):
        facilitator = django_user_model.objects.create_user(
            username=username, password="correct horse battery staple"
        )
        project = Project.objects.create(name="Cycle Project", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        return facilitator, project

    def test_creating_a_cycle_when_none_active_succeeds_in_collecting_state(
        self, client, django_user_model
    ):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        client.force_login(facilitator)

        response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 302
        assert response.url == reverse("project_detail", kwargs={"pk": project.pk})
        cycle = FeedbackCycle.objects.get(project=project)
        assert cycle.state == FeedbackCycle.State.COLLECTING
        assert cycle.week == "Cycle 1"
        assert cycle.opens_at is not None
        assert cycle.closes_at is None
        assert cycle.revealed_at is None
        assert cycle.voting_closed_at is None
        assert cycle.completed_at is None

    def test_creating_a_cycle_while_one_is_active_is_rejected(
        self, client, django_user_model
    ):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )
        client.force_login(facilitator)

        response = client.post(
            reverse("create_cycle", kwargs={"pk": project.pk}), follow=True
        )

        assert FeedbackCycle.objects.filter(project=project).count() == 1
        content = response.content.decode().lower()
        assert "already" in content and "active" in content

    def test_creating_a_cycle_is_rejected_for_every_non_closed_state(
        self, client, django_user_model
    ):
        non_closed_states = [
            state
            for state in FeedbackCycle.State
            if state != FeedbackCycle.State.CLOSED
        ]
        for state in non_closed_states:
            facilitator, project = self._make_facilitator_and_project(
                django_user_model, username=f"fac_{state}"
            )
            FeedbackCycle.objects.create(project=project, week="Cycle 1", state=state)
            client.force_login(facilitator)

            client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

            assert FeedbackCycle.objects.filter(project=project).count() == 1

    def test_creating_a_cycle_is_allowed_after_the_previous_one_is_closed(
        self, client, django_user_model
    ):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.CLOSED
        )
        client.force_login(facilitator)

        response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 302
        assert FeedbackCycle.objects.filter(project=project).count() == 2
        newest = FeedbackCycle.objects.filter(project=project).exclude(
            state=FeedbackCycle.State.CLOSED
        ).get()
        assert newest.week == "Cycle 2"

    def test_db_constraint_prevents_two_active_cycles_regardless_of_app_logic(
        self, django_user_model
    ):
        # Exercises the constraint directly at the ORM/DB layer, bypassing
        # the view's own logic entirely, to prove the "no second active
        # cycle" rule is enforced by the database (a race between two
        # check-then-create requests can't slip past it) and not only by
        # an application-level check before insert.
        facilitator = django_user_model.objects.create_user(
            username="race_fac", password="correct horse battery staple"
        )
        project = Project.objects.create(name="Race Project", created_by=facilitator)
        FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FeedbackCycle.objects.create(
                    project=project, week="Cycle 2", state=FeedbackCycle.State.COLLECTING
                )

        assert FeedbackCycle.objects.filter(project=project).count() == 1

    def test_non_facilitator_gets_404(self, client, django_user_model):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        member = django_user_model.objects.create_user(
            username="yolanda", password="correct horse battery staple"
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        client.force_login(member)

        response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 404
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_non_member_gets_404(self, client, django_user_model):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        outsider = django_user_model.objects.create_user(
            username="zack", password="correct horse battery staple"
        )
        client.force_login(outsider)

        response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 404
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_get_request_does_not_create_a_cycle(self, client, django_user_model):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        client.force_login(facilitator)

        response = client.get(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 405
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_anonymous_post_redirected_to_login_and_creates_nothing(
        self, client, django_user_model
    ):
        facilitator, project = self._make_facilitator_and_project(django_user_model)

        response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        assert not FeedbackCycle.objects.filter(project=project).exists()

    def test_after_creating_facilitator_lands_on_project_page_showing_collecting_state(
        self, client, django_user_model
    ):
        facilitator, project = self._make_facilitator_and_project(django_user_model)
        client.force_login(facilitator)

        response = client.post(
            reverse("create_cycle", kwargs={"pk": project.pk}), follow=True
        )

        assert response.status_code == 200
        assert response.redirect_chain[-1][0] == reverse(
            "project_detail", kwargs={"pk": project.pk}
        )
        content = response.content.decode().lower()
        assert "cycle 1" in content
        assert "collecting" in content
