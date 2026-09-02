import re
from unittest.mock import patch

import pytest
from django.urls import reverse

from projects.models import JOIN_CODE_ALPHABET, Membership, Project

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
        assert response.url == reverse("project_created", kwargs={"pk": project.pk})
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
        assert response.url == reverse("project_created", kwargs={"pk": project.pk})
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
class TestProjectCreatedConfirmation:
    def test_shows_project_name_and_join_code(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="henry", password="correct horse battery staple"
        )
        client.force_login(user)
        project = Project.objects.create(name="Confirm Me", created_by=user)

        response = client.get(reverse("project_created", kwargs={"pk": project.pk}))

        assert response.status_code == 200
        content = response.content.decode()
        assert "Confirm Me" in content
        assert project.join_code in content
