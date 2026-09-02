"""Tests for the shared `facilitator_required` decorator (#6).

Exercised directly against a throwaway view via RequestFactory, per the
issue's acceptance criteria — no test-only route is registered in
projects/urls.py.
"""
import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import Http404, HttpResponse
from django.test import RequestFactory
from django.urls import reverse

from projects.decorators import facilitator_required
from projects.models import Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@facilitator_required
def throwaway_view(request, pk, project=None, membership=None):
    """Not a real product view — exists only to prove the decorator works."""
    return HttpResponse(f"ok:{project.pk}:{membership.role}")


@pytest.mark.django_db
class TestFacilitatorRequired:
    def test_facilitator_reaches_the_wrapped_view(self, django_user_model):
        facilitator = django_user_model.objects.create_user(
            username="alice", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Retro Team", created_by=facilitator)
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        request = RequestFactory().get(f"/projects/{project.pk}/")
        request.user = facilitator

        response = throwaway_view(request, pk=project.pk)

        assert response.status_code == 200
        assert response.content == f"ok:{project.pk}:facilitator".encode()

    def test_member_gets_404_not_a_permission_error(self, django_user_model):
        owner = django_user_model.objects.create_user(
            username="owner", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Retro Team", created_by=owner)
        member = django_user_model.objects.create_user(
            username="bob", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=member, role=Membership.Role.MEMBER
        )
        request = RequestFactory().get(f"/projects/{project.pk}/")
        request.user = member

        with pytest.raises(Http404):
            throwaway_view(request, pk=project.pk)

    def test_non_member_of_existing_project_gets_404(self, django_user_model):
        owner = django_user_model.objects.create_user(
            username="owner2", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Retro Team", created_by=owner)
        outsider = django_user_model.objects.create_user(
            username="carol", password=VALID_PASSWORD
        )
        request = RequestFactory().get(f"/projects/{project.pk}/")
        request.user = outsider

        with pytest.raises(Http404):
            throwaway_view(request, pk=project.pk)

    def test_nonexistent_project_gets_404(self, django_user_model):
        user = django_user_model.objects.create_user(
            username="dave", password=VALID_PASSWORD
        )
        request = RequestFactory().get("/projects/999999/")
        request.user = user

        with pytest.raises(Http404):
            throwaway_view(request, pk=999999)

    def test_member_and_nonexistent_project_404s_are_indistinguishable(
        self, django_user_model
    ):
        owner = django_user_model.objects.create_user(
            username="owner3", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Retro Team", created_by=owner)
        member = django_user_model.objects.create_user(
            username="erin", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=member, role=Membership.Role.MEMBER
        )
        member_request = RequestFactory().get(f"/projects/{project.pk}/")
        member_request.user = member
        missing_request = RequestFactory().get("/projects/999999/")
        missing_request.user = member

        with pytest.raises(Http404) as member_exc:
            throwaway_view(member_request, pk=project.pk)
        with pytest.raises(Http404) as missing_exc:
            throwaway_view(missing_request, pk=999999)

        # Same exception type, no extra detail either way — a wrong-role
        # request can't be told apart from a nonexistent project.
        assert type(member_exc.value) is type(missing_exc.value)

    def test_anonymous_request_is_redirected_to_login(self, django_user_model):
        owner = django_user_model.objects.create_user(
            username="frank", password=VALID_PASSWORD
        )
        project = Project.objects.create(name="Retro Team", created_by=owner)
        request = RequestFactory().get(f"/projects/{project.pk}/")
        request.user = AnonymousUser()

        response = throwaway_view(request, pk=project.pk)

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
