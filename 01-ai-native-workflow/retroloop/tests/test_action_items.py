"""Tests for #24: action item tracking on the project page.

Mirrors tests/test_review_drafts.py and tests/test_cycle_summary.py for
project/facilitator/member setup and the facilitator-only vs. plain-member
gating style, except toggle_action_item_status itself uses neither pattern
— it's a plain ownership check (request.user == item.owner), per #24's own
decision that this permission is granted to the owner specifically, not
the facilitator generically.

The issue's own explicitly required test lives in
TestProjectPageOpenActionItemsList.test_toggle_updates_the_project_page_
list_immediately: the owner can toggle their item; another member (and the
facilitator, unless they happen to also be the owner) cannot; the project
page's open-items list reflects the toggle immediately.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    ActionItem,
    DraftSource,
    FeedbackCycle,
    Membership,
    Project,
    RetrospectiveSummary,
)

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class ActionItemTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"aifac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Action Item Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"aimem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_published_cycle(self, project, week="Cycle 1"):
        """A closed cycle with a published RetrospectiveSummary —
        completed_at is set in the same request publish_summary uses (#23),
        so this mirrors that exactly rather than setting completed_at by
        itself.
        """
        cycle = FeedbackCycle.objects.create(
            project=project,
            week=week,
            state=FeedbackCycle.State.CLOSED,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
            completed_at=timezone.now(),
        )
        RetrospectiveSummary.objects.create(cycle=cycle)
        return cycle

    def _make_active_cycle(self, project, week="Cycle 1", **overrides):
        defaults = dict(
            project=project,
            week=week,
            state=FeedbackCycle.State.DISCUSSING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )
        defaults.update(overrides)
        return FeedbackCycle.objects.create(**defaults)

    def _make_confirmed_action_item(
        self, cycle, *, owner=None, description="Follow up with the vendor.", **kwargs
    ):
        kwargs.setdefault("confirmed_at", timezone.now())
        return ActionItem.objects.create(
            cycle=cycle,
            description=description,
            owner=owner,
            source=DraftSource.AI,
            **kwargs,
        )

    def _project_url(self, project):
        return reverse("project_detail", kwargs={"pk": project.pk})

    def _toggle_url(self, project, item):
        return reverse(
            "toggle_action_item_status", kwargs={"pk": project.pk, "item_id": item.pk}
        )


# -- Toggling: only the assigned owner can, including against the
# facilitator specifically (per #24's decision, a different permission
# model from @facilitator_required elsewhere in this codebase). --


class TestToggleActionItemStatus(ActionItemTestBase):
    def test_owner_can_toggle_open_to_done(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)
        client.force_login(member)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 302
        assert response.url == self._project_url(project)
        item.refresh_from_db()
        assert item.status == ActionItem.Status.DONE

    def test_owner_can_toggle_done_back_to_open(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(
            cycle, owner=member, status=ActionItem.Status.DONE
        )
        client.force_login(member)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_another_member_cannot_toggle_and_gets_404(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        other_member = django_user_model.objects.create_user(
            username="aiother3", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=other_member, role=Membership.Role.MEMBER
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)
        client.force_login(other_member)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_facilitator_cannot_toggle_a_member_owned_item_and_gets_404(
        self, client, django_user_model
    ):
        # The key permission-model departure #24 calls out explicitly:
        # unlike every other mutation in this codebase, the facilitator
        # gets no special access here.
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)
        client.force_login(facilitator)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_facilitator_can_toggle_when_they_are_the_owner(self, client, django_user_model):
        # Ownership, not role, is what's checked — a facilitator who is
        # also the assigned owner is allowed, same as any other owner.
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=facilitator)
        client.force_login(facilitator)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.status == ActionItem.Status.DONE

    def test_unowned_item_cannot_be_toggled_by_anyone(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=None)

        for user in (facilitator, member):
            client.force_login(user)
            response = client.post(self._toggle_url(project, item))
            assert response.status_code == 404

        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_unconfirmed_item_cannot_be_toggled_even_by_its_matched_owner(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member, confirmed_at=None)
        client.force_login(member)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_item_from_another_project_cannot_be_toggled(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        _other_facilitator, other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "8b")
        )
        other_cycle = self._make_published_cycle(other_project)
        other_item = self._make_confirmed_action_item(other_cycle, owner=other_member)
        client.force_login(member)

        response = client.post(
            reverse(
                "toggle_action_item_status",
                kwargs={"pk": project.pk, "item_id": other_item.pk},
            )
        )

        assert response.status_code == 404
        other_item.refresh_from_db()
        assert other_item.status == ActionItem.Status.OPEN

    def test_non_member_gets_404(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)
        outsider = django_user_model.objects.create_user(
            username="outsider9", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_anonymous_post_is_redirected_to_login(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)

        response = client.post(self._toggle_url(project, item))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(cycle, owner=member)
        client.force_login(member)

        response = client.get(self._toggle_url(project, item))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_toggle", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(
            reverse(
                "toggle_action_item_status", kwargs={"pk": 999999, "item_id": 1}
            )
        )

        assert response.status_code == 404


# -- The project page's open action items list --


class TestProjectPageOpenActionItemsList(ActionItemTestBase):
    def test_lists_open_items_from_published_cycles(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_published_cycle(project)
        self._make_confirmed_action_item(
            cycle, owner=member, description="Write the runbook."
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Write the runbook." in content

    def test_lists_open_items_across_multiple_published_cycles(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle_1 = self._make_published_cycle(project, week="Cycle 1")
        cycle_2 = self._make_published_cycle(project, week="Cycle 2")
        self._make_confirmed_action_item(
            cycle_1, owner=member, description="From cycle one."
        )
        self._make_confirmed_action_item(
            cycle_2, owner=member, description="From cycle two."
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))
        content = response.content.decode()

        assert "From cycle one." in content
        assert "From cycle two." in content

    def test_done_items_are_not_listed(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_published_cycle(project)
        self._make_confirmed_action_item(
            cycle,
            owner=member,
            description="Already finished.",
            status=ActionItem.Status.DONE,
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))

        assert "Already finished." not in response.content.decode()

    def test_items_from_an_unpublished_active_cycle_are_not_listed(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "23"
        )
        active_cycle = self._make_active_cycle(project)
        self._make_confirmed_action_item(
            active_cycle, owner=member, description="Not published yet."
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))

        assert "Not published yet." not in response.content.decode()

    def test_unconfirmed_items_from_a_published_cycle_are_not_listed(
        self, client, django_user_model
    ):
        # Not reachable through this app's own UI (publish_summary never
        # requires every draft to be reviewed first), but guarded against
        # anyway per ActionItem.objects.open_for_published_cycles' own
        # docstring.
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "24"
        )
        cycle = self._make_published_cycle(project)
        self._make_confirmed_action_item(
            cycle,
            owner=member,
            description="Never reviewed.",
            confirmed_at=None,
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))

        assert "Never reviewed." not in response.content.decode()

    def test_items_from_another_project_are_not_listed(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "25"
        )
        _other_facilitator, other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "25b")
        )
        other_cycle = self._make_published_cycle(other_project)
        self._make_confirmed_action_item(
            other_cycle, owner=other_member, description="Belongs elsewhere."
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))

        assert "Belongs elsewhere." not in response.content.decode()

    def test_unassigned_open_item_is_listed_but_has_no_toggle_control(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "26"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(
            cycle, owner=None, description="Nobody assigned yet."
        )
        client.force_login(facilitator)

        response = client.get(self._project_url(project))
        content = response.content.decode()

        assert "Nobody assigned yet." in content
        assert reverse(
            "toggle_action_item_status", kwargs={"pk": project.pk, "item_id": item.pk}
        ) not in content

    # -- The issue's own explicitly required test --

    def test_toggle_updates_the_project_page_list_immediately(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "27"
        )
        other_member = django_user_model.objects.create_user(
            username="aiother27", password=VALID_PASSWORD
        )
        Membership.objects.create(
            project=project, user=other_member, role=Membership.Role.MEMBER
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(
            cycle, owner=member, description="Update the deploy docs."
        )

        # Before toggling, the item is on the list.
        client.force_login(facilitator)
        before_response = client.get(self._project_url(project))
        assert "Update the deploy docs." in before_response.content.decode()

        # A non-owner member cannot toggle it — including the facilitator.
        client.force_login(other_member)
        denied_response = client.post(self._toggle_url(project, item))
        assert denied_response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

        client.force_login(facilitator)
        denied_response = client.post(self._toggle_url(project, item))
        assert denied_response.status_code == 404
        item.refresh_from_db()
        assert item.status == ActionItem.Status.OPEN

        # The owner toggles it done.
        client.force_login(member)
        toggle_response = client.post(self._toggle_url(project, item))
        assert toggle_response.status_code == 302
        item.refresh_from_db()
        assert item.status == ActionItem.Status.DONE

        # It's gone from the list immediately — no extra step needed.
        after_response = client.get(self._project_url(project))
        assert "Update the deploy docs." not in after_response.content.decode()

    def test_owner_can_mark_their_own_item_done_from_the_project_page_form(
        self, client, django_user_model
    ):
        # The project page itself renders a toggle form only for the
        # viewer's own item — exercised end to end via the rendered form's
        # action URL, not just the bare endpoint.
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "28"
        )
        cycle = self._make_published_cycle(project)
        item = self._make_confirmed_action_item(
            cycle, owner=member, description="Owned by the viewer."
        )
        client.force_login(member)

        response = client.get(self._project_url(project))
        content = response.content.decode()
        toggle_action_url = reverse(
            "toggle_action_item_status", kwargs={"pk": project.pk, "item_id": item.pk}
        )

        assert toggle_action_url in content

        toggle_response = client.post(toggle_action_url)
        assert toggle_response.status_code == 302
        item.refresh_from_db()
        assert item.status == ActionItem.Status.DONE
