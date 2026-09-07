"""Tests for #25: the project page's "previous retrospectives" list.

The underlying query (``published_cycles`` in ``project_detail``) and its
template rendering were already built as part of #23's work (publishing a
summary is what makes a cycle eligible to show up here at all), but #25's
own acceptance criteria — newest-first ordering, excluding a cycle with no
published summary, and each entry linking to its summary — had no dedicated
test coverage yet. This file adds it directly against the existing
implementation.

Mirrors tests/test_cycle_summary.py for fixture conventions.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class PreviousRetrospectivesTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"prevfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Previous Retros Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"prevmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_revealed_cycle(self, project, week="Cycle 1"):
        return FeedbackCycle.objects.create(
            project=project,
            week=week,
            state=FeedbackCycle.State.DISCUSSING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )

    def _publish(self, client, project, cycle):
        return client.post(
            reverse("publish_summary", kwargs={"pk": project.pk, "cycle_id": cycle.pk})
        )

    def _detail_url(self, project):
        return reverse("project_detail", kwargs={"pk": project.pk})

    def _summary_url(self, project, cycle):
        return reverse("cycle_summary", kwargs={"pk": project.pk, "cycle_id": cycle.pk})


class TestPreviousRetrospectivesList(PreviousRetrospectivesTestBase):
    def test_active_unpublished_cycle_does_not_appear(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._detail_url(project))
        content = response.content.decode()

        assert "No published retrospectives yet." in content

    def test_publishing_makes_the_cycle_appear(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_revealed_cycle(project, week="Cycle 1")
        client.force_login(facilitator)
        self._publish(client, project, cycle)

        # Both a facilitator and an ordinary member should see it.
        for viewer in (facilitator, member):
            client.force_login(viewer)
            response = client.get(self._detail_url(project))
            content = response.content.decode()

            assert "Cycle 1" in content
            assert "No published retrospectives yet." not in content
            assert self._summary_url(project, cycle) in content

    def test_closed_cycle_without_a_published_summary_does_not_appear(
        self, client, django_user_model
    ):
        # Not reachable through this app's own UI (closing only ever
        # happens via publish_summary, which always creates the summary in
        # the same request) — but the query is expected to simply exclude
        # a cycle like this rather than erroring, per the issue's own
        # decision.
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        FeedbackCycle.objects.create(
            project=project,
            week="Orphaned closed cycle",
            state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
            completed_at=timezone.now(),
        )
        client.force_login(facilitator)

        response = client.get(self._detail_url(project))
        content = response.content.decode()

        assert "Orphaned closed cycle" not in content
        assert "No published retrospectives yet." in content

    def test_multiple_published_cycles_are_listed_newest_first(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        client.force_login(facilitator)

        first_cycle = self._make_revealed_cycle(project, week="Cycle 1")
        self._publish(client, project, first_cycle)

        second_cycle = FeedbackCycle.objects.create(
            project=project,
            week="Cycle 2",
            state=FeedbackCycle.State.DISCUSSING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )
        self._publish(client, project, second_cycle)

        response = client.get(self._detail_url(project))
        content = response.content.decode()

        first_index = content.index("Cycle 1")
        second_index = content.index("Cycle 2")
        # Most recently published at the top: Cycle 2 (published second)
        # appears before Cycle 1 (published first).
        assert second_index < first_index

    def test_a_project_with_no_cycles_at_all_shows_the_empty_message(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        client.force_login(facilitator)

        response = client.get(self._detail_url(project))
        content = response.content.decode()

        assert "No published retrospectives yet." in content

    def test_published_cycles_from_another_project_are_never_listed(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        _other_facilitator, _other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "6b")
        )
        other_cycle = self._make_revealed_cycle(other_project, week="Other project's cycle")
        client.force_login(_other_facilitator)
        self._publish(client, other_project, other_cycle)

        client.force_login(facilitator)
        response = client.get(self._detail_url(project))
        content = response.content.decode()

        assert "Other project's cycle" not in content
