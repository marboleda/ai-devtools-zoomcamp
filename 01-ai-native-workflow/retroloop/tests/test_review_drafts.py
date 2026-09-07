"""Tests for #22: the facilitator-only screen that reviews, edits,
confirms, and discards AI-suggested DecisionDraft/ActionItem rows (from
#21), and lets the facilitator add a brand-new manual row directly.

Mirrors tests/test_close_voting.py and tests/test_reveal.py for the
purely facilitator-gated screen/action pattern (unlike
tests/test_discussion.py, nothing here is reachable by a plain member).

The issue's own explicitly required test lives in
TestConfirmedForCycleQueryPath below: an unconfirmed draft never appears
through DecisionDraft.objects.confirmed_for_cycle /
ActionItem.objects.confirmed_for_cycle (the query path #23's published
summary is expected to use); confirming it makes it eligible; discarding
it removes it entirely.
"""
import datetime

import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import ActionItem, DecisionDraft, DraftSource, FeedbackCycle, Membership, Project

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class ReviewDraftsTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"rdfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Review Drafts Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"rdmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_active_cycle(self, project, *, closed=False):
        return FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.CLOSED if closed else FeedbackCycle.State.DISCUSSING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )

    def _make_decision_draft(self, cycle, text="Ship the fix.", **kwargs):
        return DecisionDraft.objects.create(
            cycle=cycle, text=text, source=DraftSource.AI, **kwargs
        )

    def _make_action_item(self, cycle, description="Fix the flaky test.", **kwargs):
        return ActionItem.objects.create(
            cycle=cycle, description=description, source=DraftSource.AI, **kwargs
        )

    def _review_url(self, project):
        return reverse("review_drafts", kwargs={"pk": project.pk})

    def _confirm_decision_url(self, project, draft):
        return reverse(
            "confirm_decision_draft", kwargs={"pk": project.pk, "draft_id": draft.pk}
        )

    def _discard_decision_url(self, project, draft):
        return reverse(
            "discard_decision_draft", kwargs={"pk": project.pk, "draft_id": draft.pk}
        )

    def _create_decision_url(self, project):
        return reverse("create_decision_draft", kwargs={"pk": project.pk})

    def _confirm_action_url(self, project, item):
        return reverse(
            "confirm_action_item", kwargs={"pk": project.pk, "item_id": item.pk}
        )

    def _discard_action_url(self, project, item):
        return reverse(
            "discard_action_item", kwargs={"pk": project.pk, "item_id": item.pk}
        )

    def _create_action_url(self, project):
        return reverse("create_action_item", kwargs={"pk": project.pk})


# -- The review screen itself: facilitator-only, lists unconfirmed rows --


class TestReviewDraftsScreen(ReviewDraftsTestBase):
    def test_facilitator_sees_unconfirmed_drafts_and_action_items(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle, text="Ship the fix on Friday.")
        item = self._make_action_item(cycle, description="Investigate the outage.")
        client.force_login(facilitator)

        response = client.get(self._review_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Ship the fix on Friday." in content
        assert "Investigate the outage." in content
        assert draft.confirmed_at is None
        assert item.confirmed_at is None

    def test_confirmed_rows_are_not_listed_on_the_review_screen(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_active_cycle(project)
        self._make_decision_draft(
            cycle,
            text="Already confirmed decision.",
            confirmed_at=timezone.now(),
            confirmed_by=facilitator,
        )
        self._make_action_item(
            cycle,
            description="Already confirmed action item.",
            confirmed_at=timezone.now(),
            confirmed_by=facilitator,
        )
        client.force_login(facilitator)

        response = client.get(self._review_url(project))
        content = response.content.decode()

        assert "Already confirmed decision." not in content
        assert "Already confirmed action item." not in content

    def test_rows_from_a_different_project_are_never_listed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        _other_facilitator, _other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "3b")
        )
        self._make_active_cycle(project)
        other_cycle = self._make_active_cycle(other_project)
        self._make_decision_draft(other_cycle, text="Belongs to the other project.")
        client.force_login(facilitator)

        response = client.get(self._review_url(project))

        assert "Belongs to the other project." not in response.content.decode()

    def test_member_cannot_view_the_review_screen_and_gets_404(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.get(self._review_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        self._make_active_cycle(project)

        response = client.get(self._review_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_review", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("review_drafts", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_no_active_cycle_is_a_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        client.force_login(facilitator)

        response = client.get(self._review_url(project))

        assert response.status_code == 404


# -- Confirming a decision draft --


class TestConfirmDecisionDraft(ReviewDraftsTestBase):
    def test_facilitator_can_confirm_a_decision_draft_as_is(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle, text="Original decision text.")
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(
            self._confirm_decision_url(project, draft), {"text": "Original decision text."}
        )

        assert response.status_code == 302
        draft.refresh_from_db()
        assert draft.confirmed_at is not None
        assert draft.confirmed_at >= before
        assert draft.confirmed_by == facilitator
        assert draft.text == "Original decision text."

    def test_facilitator_can_edit_the_text_before_confirming(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle, text="Original decision text.")
        client.force_login(facilitator)

        response = client.post(
            self._confirm_decision_url(project, draft), {"text": "Edited decision text."}
        )

        assert response.status_code == 302
        draft.refresh_from_db()
        assert draft.text == "Edited decision text."
        assert draft.confirmed_at is not None
        assert draft.confirmed_by == facilitator

    def test_blank_text_does_not_confirm_the_draft(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle, text="Keep me.")
        client.force_login(facilitator)

        response = client.post(self._confirm_decision_url(project, draft), {"text": ""})

        assert response.status_code == 302
        draft.refresh_from_db()
        assert draft.confirmed_at is None
        assert draft.text == "Keep me."

    def test_member_cannot_confirm_a_decision_draft_and_gets_404(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "13"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle)
        client.force_login(member)

        response = client.post(
            self._confirm_decision_url(project, draft), {"text": "Hijacked."}
        )

        assert response.status_code == 404
        draft.refresh_from_db()
        assert draft.confirmed_at is None

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "14"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle)
        client.force_login(facilitator)

        response = client.get(self._confirm_decision_url(project, draft))

        assert response.status_code == 405

    def test_confirming_an_already_confirmed_draft_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "15"
        )
        cycle = self._make_active_cycle(project)
        first_confirmed_at = timezone.now()
        draft = self._make_decision_draft(
            cycle,
            text="Already confirmed.",
            confirmed_at=first_confirmed_at,
            confirmed_by=facilitator,
        )
        client.force_login(facilitator)

        response = client.post(
            self._confirm_decision_url(project, draft), {"text": "Trying to re-confirm."}
        )

        assert response.status_code == 404
        draft.refresh_from_db()
        assert draft.confirmed_at == first_confirmed_at
        assert draft.text == "Already confirmed."

    def test_a_draft_from_another_project_cannot_be_confirmed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "16"
        )
        _other_facilitator, _other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "16b")
        )
        self._make_active_cycle(project)
        other_cycle = self._make_active_cycle(other_project)
        other_draft = self._make_decision_draft(other_cycle, text="Not this project's.")
        client.force_login(facilitator)

        response = client.post(
            self._confirm_decision_url(project, other_draft), {"text": "Hijacked."}
        )

        assert response.status_code == 404
        other_draft.refresh_from_db()
        assert other_draft.confirmed_at is None


# -- Discarding a decision draft (hard delete) --


class TestDiscardDecisionDraft(ReviewDraftsTestBase):
    def test_facilitator_can_discard_an_unconfirmed_draft(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle)
        client.force_login(facilitator)

        response = client.post(self._discard_decision_url(project, draft))

        assert response.status_code == 302
        assert not DecisionDraft.objects.filter(pk=draft.pk).exists()

    def test_discarding_an_already_confirmed_draft_is_rejected_and_keeps_the_row(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(
            cycle, confirmed_at=timezone.now(), confirmed_by=facilitator
        )
        client.force_login(facilitator)

        response = client.post(self._discard_decision_url(project, draft))

        assert response.status_code == 404
        assert DecisionDraft.objects.filter(pk=draft.pk).exists()

    def test_member_cannot_discard_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle)
        client.force_login(member)

        response = client.post(self._discard_decision_url(project, draft))

        assert response.status_code == 404
        assert DecisionDraft.objects.filter(pk=draft.pk).exists()


# -- Adding a brand-new manual decision --


class TestCreateManualDecision(ReviewDraftsTestBase):
    def test_facilitator_can_add_a_manual_decision_confirmed_immediately(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "30"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(
            self._create_decision_url(project), {"text": "Decided live in the meeting."}
        )

        assert response.status_code == 302
        draft = DecisionDraft.objects.get(cycle=cycle)
        assert draft.text == "Decided live in the meeting."
        assert draft.source == DraftSource.MANUAL
        assert draft.confirmed_at is not None
        assert draft.confirmed_at >= before
        assert draft.confirmed_by == facilitator

    def test_blank_text_creates_nothing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "31"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._create_decision_url(project), {"text": "   "})

        assert response.status_code == 302
        assert DecisionDraft.objects.count() == 0

    def test_member_cannot_add_a_manual_decision(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "32"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.post(
            self._create_decision_url(project), {"text": "Should not be created."}
        )

        assert response.status_code == 404
        assert DecisionDraft.objects.count() == 0


# -- Confirming an action item, including owner/due date edits --


class TestConfirmActionItem(ReviewDraftsTestBase):
    def test_facilitator_can_confirm_an_action_item_as_is(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "40"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(self._confirm_action_url(project, item), {})

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.confirmed_at is not None
        assert item.confirmed_at >= before
        assert item.confirmed_by == facilitator

    def test_facilitator_can_set_owner_and_due_date_before_confirming(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "41"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(facilitator)

        response = client.post(
            self._confirm_action_url(project, item),
            {"owner": member.pk, "due_date": "2026-09-30"},
        )

        assert response.status_code == 302
        item.refresh_from_db()
        assert item.owner == member
        assert item.due_date == datetime.date(2026, 9, 30)
        assert item.confirmed_at is not None
        assert item.confirmed_by == facilitator

    def test_owner_must_be_a_project_member(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "42"
        )
        _other_facilitator, outsider, _other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "42b")
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(facilitator)

        response = client.post(
            self._confirm_action_url(project, item), {"owner": outsider.pk}
        )

        assert response.status_code == 302
        item.refresh_from_db()
        # The outsider isn't a valid choice, so the form is invalid and the
        # item is left exactly as it was — unconfirmed, no owner set.
        assert item.confirmed_at is None
        assert item.owner is None

    def test_member_cannot_confirm_an_action_item_and_gets_404(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "43"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(member)

        response = client.post(self._confirm_action_url(project, item), {})

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.confirmed_at is None

    def test_confirming_an_already_confirmed_action_item_is_rejected(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "44"
        )
        cycle = self._make_active_cycle(project)
        first_confirmed_at = timezone.now()
        item = self._make_action_item(
            cycle, confirmed_at=first_confirmed_at, confirmed_by=facilitator
        )
        client.force_login(facilitator)

        response = client.post(self._confirm_action_url(project, item), {})

        assert response.status_code == 404
        item.refresh_from_db()
        assert item.confirmed_at == first_confirmed_at


# -- Discarding an action item (hard delete) --


class TestDiscardActionItem(ReviewDraftsTestBase):
    def test_facilitator_can_discard_an_unconfirmed_action_item(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "50"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(facilitator)

        response = client.post(self._discard_action_url(project, item))

        assert response.status_code == 302
        assert not ActionItem.objects.filter(pk=item.pk).exists()

    def test_member_cannot_discard_an_action_item(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "51"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle)
        client.force_login(member)

        response = client.post(self._discard_action_url(project, item))

        assert response.status_code == 404
        assert ActionItem.objects.filter(pk=item.pk).exists()


# -- Adding a brand-new manual action item --


class TestCreateManualActionItem(ReviewDraftsTestBase):
    def test_facilitator_can_add_a_manual_action_item_confirmed_immediately(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "60"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(
            self._create_action_url(project),
            {
                "description": "Follow up with the vendor.",
                "owner": member.pk,
                "due_date": "2026-10-01",
            },
        )

        assert response.status_code == 302
        item = ActionItem.objects.get(cycle=cycle)
        assert item.description == "Follow up with the vendor."
        assert item.owner == member
        assert item.due_date == datetime.date(2026, 10, 1)
        assert item.source == DraftSource.MANUAL
        assert item.confirmed_at is not None
        assert item.confirmed_at >= before
        assert item.confirmed_by == facilitator
        assert item.status == ActionItem.Status.OPEN

    def test_blank_description_creates_nothing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "61"
        )
        self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._create_action_url(project), {"description": ""})

        assert response.status_code == 302
        assert ActionItem.objects.count() == 0

    def test_owner_and_due_date_are_optional(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "62"
        )
        cycle = self._make_active_cycle(project)
        client.force_login(facilitator)

        response = client.post(
            self._create_action_url(project), {"description": "No owner yet."}
        )

        assert response.status_code == 302
        item = ActionItem.objects.get(cycle=cycle)
        assert item.owner is None
        assert item.due_date is None
        assert item.confirmed_at is not None

    def test_member_cannot_add_a_manual_action_item(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "63"
        )
        self._make_active_cycle(project)
        client.force_login(member)

        response = client.post(
            self._create_action_url(project), {"description": "Should not be created."}
        )

        assert response.status_code == 404
        assert ActionItem.objects.count() == 0


# -- The issue's own explicitly required test: an unconfirmed draft never
# appears through the summary's query path (confirmed_for_cycle); confirming
# it makes it eligible; discarding it removes it entirely. Tested directly
# against DecisionDraft.objects.confirmed_for_cycle /
# ActionItem.objects.confirmed_for_cycle — the query path #23's published
# summary is expected to read from — not against #23's own (not-yet-built)
# view.


class TestConfirmedForCycleQueryPath(ReviewDraftsTestBase):
    def test_unconfirmed_decision_draft_is_excluded_confirming_includes_it_discarding_removes_it(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "70"
        )
        cycle = self._make_active_cycle(project)
        draft = self._make_decision_draft(cycle, text="Ship the fix on Friday.")

        # Unconfirmed: excluded from the eligible-for-publishing query.
        assert draft not in DecisionDraft.objects.confirmed_for_cycle(cycle=cycle)
        assert draft in DecisionDraft.objects.unconfirmed_for_cycle(cycle=cycle)

        client.force_login(facilitator)
        response = client.post(
            self._confirm_decision_url(project, draft), {"text": "Ship the fix on Friday."}
        )
        assert response.status_code == 302

        # Confirmed: now eligible.
        assert draft in DecisionDraft.objects.confirmed_for_cycle(cycle=cycle)
        assert draft not in DecisionDraft.objects.unconfirmed_for_cycle(cycle=cycle)

        # A second, separate unconfirmed draft: discarding removes it
        # entirely — not just from the eligible query, from the database.
        other_draft = self._make_decision_draft(cycle, text="A draft nobody wants.")
        assert other_draft not in DecisionDraft.objects.confirmed_for_cycle(cycle=cycle)

        client.post(self._discard_decision_url(project, other_draft))

        assert not DecisionDraft.objects.filter(pk=other_draft.pk).exists()
        assert other_draft not in DecisionDraft.objects.confirmed_for_cycle(cycle=cycle)

    def test_unconfirmed_action_item_is_excluded_confirming_includes_it_discarding_removes_it(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "71"
        )
        cycle = self._make_active_cycle(project)
        item = self._make_action_item(cycle, description="Fix the flaky test.")

        assert item not in ActionItem.objects.confirmed_for_cycle(cycle=cycle)
        assert item in ActionItem.objects.unconfirmed_for_cycle(cycle=cycle)

        client.force_login(facilitator)
        response = client.post(self._confirm_action_url(project, item), {})
        assert response.status_code == 302

        assert item in ActionItem.objects.confirmed_for_cycle(cycle=cycle)
        assert item not in ActionItem.objects.unconfirmed_for_cycle(cycle=cycle)

        other_item = self._make_action_item(cycle, description="Nobody wants this one.")
        assert other_item not in ActionItem.objects.confirmed_for_cycle(cycle=cycle)

        client.post(self._discard_action_url(project, other_item))

        assert not ActionItem.objects.filter(pk=other_item.pk).exists()
        assert other_item not in ActionItem.objects.confirmed_for_cycle(cycle=cycle)

    def test_confirmed_for_cycle_never_leaks_rows_from_another_cycle(
        self, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "72"
        )
        cycle_a = self._make_active_cycle(project, closed=True)
        cycle_b = FeedbackCycle.objects.create(
            project=project, week="Cycle 2", state=FeedbackCycle.State.DISCUSSING
        )
        self._make_decision_draft(
            cycle_a,
            text="Confirmed in a different cycle.",
            confirmed_at=timezone.now(),
            confirmed_by=facilitator,
        )

        assert (
            DecisionDraft.objects.confirmed_for_cycle(cycle=cycle_b).count() == 0
        )
