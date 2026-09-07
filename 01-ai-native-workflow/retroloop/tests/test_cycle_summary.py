"""Tests for #23: the retrospective summary screen and the facilitator-only
action that publishes it.

Mirrors tests/test_close_voting.py and tests/test_review_drafts.py for the
facilitator-only, one-shot-action pattern (publish_summary), and
tests/test_board_reveal.py for the card-anonymity assertion style
(cycle_summary shows the original feedback cards).

The issue's two explicitly required tests live in
TestPublishedSummaryOnlyShowsConfirmedRows (an unconfirmed decision/action
item never appears in the published summary; a confirmed one does) and
TestCreateCycleAfterPublishing (after publishing, #7's create-cycle action
succeeds again for the same project).
"""
import pytest
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    ActionItem,
    Card,
    Cluster,
    CycleParticipation,
    DecisionDraft,
    DiscussionTopic,
    DraftSource,
    FeedbackCycle,
    MeetingRecord,
    Membership,
    Project,
    RetrospectiveSummary,
)

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class SummaryTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"sumfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Summary Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"summem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_revealed_cycle(self, project, **overrides):
        defaults = dict(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.DISCUSSING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now(),
        )
        defaults.update(overrides)
        return FeedbackCycle.objects.create(**defaults)

    def _summary_url(self, project, cycle):
        return reverse("cycle_summary", kwargs={"pk": project.pk, "cycle_id": cycle.pk})

    def _publish_url(self, project, cycle):
        return reverse("publish_summary", kwargs={"pk": project.pk, "cycle_id": cycle.pk})


# -- The summary screen: viewing access before/after publication --


class TestCycleSummaryScreenAccess(SummaryTestBase):
    def test_facilitator_can_preview_before_publishing(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Publish summary" in content

    def test_member_cannot_preview_before_publishing_and_gets_404(
        self, client, django_user_model
    ):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(member)

        response = client.get(self._summary_url(project, cycle))

        assert response.status_code == 404

    def test_member_can_view_after_publishing(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        cycle = self._make_revealed_cycle(project)
        RetrospectiveSummary.objects.create(cycle=cycle, body="Great retro.")

        client.force_login(member)
        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Great retro." in content
        # Publishing already happened — no publish form for a member, and
        # not even shown to the facilitator once published (see below).
        assert "Publish summary" not in content

    def test_facilitator_no_longer_sees_publish_form_once_published(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        cycle = self._make_revealed_cycle(project)
        RetrospectiveSummary.objects.create(cycle=cycle)
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Publish summary" not in content

    def test_unrevealed_cycle_is_a_404_for_facilitator_and_member(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        cycle = FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

        for user in (facilitator, member):
            client.force_login(user)
            response = client.get(self._summary_url(project, cycle))
            assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )
        cycle = self._make_revealed_cycle(project)

        response = client.get(self._summary_url(project, cycle))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_summary", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(
            reverse("cycle_summary", kwargs={"pk": 999999, "cycle_id": 1})
        )

        assert response.status_code == 404

    def test_cycle_from_another_project_gets_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        _other_facilitator, _other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "7b")
        )
        other_cycle = self._make_revealed_cycle(other_project)
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, other_cycle))

        assert response.status_code == 404

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        cycle = self._make_revealed_cycle(project)
        outsider = django_user_model.objects.create_user(
            username="outsider8", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.get(self._summary_url(project, cycle))

        assert response.status_code == 404


# -- The screen's content: topics, notes, participation --


class TestCycleSummaryContent(SummaryTestBase):
    def test_shows_discussion_topics_in_rank_order_with_notes(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        cycle = self._make_revealed_cycle(project)
        cluster_a = Cluster.objects.create(
            cycle=cycle, name="Cluster A", origin=Cluster.Origin.HUMAN
        )
        cluster_b = Cluster.objects.create(
            cycle=cycle, name="Cluster B", origin=Cluster.Origin.HUMAN
        )
        DiscussionTopic.objects.create(
            cluster=cluster_b, rank=2, outcome="discussed", notes="Second topic notes."
        )
        DiscussionTopic.objects.create(
            cluster=cluster_a, rank=1, outcome="discussed", notes="First topic notes."
        )
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Cluster A" in content
        assert "Cluster B" in content
        assert "First topic notes." in content
        assert "Second topic notes." in content
        assert content.index("Cluster A") < content.index("Cluster B")

    def test_shows_attendance_participation(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        cycle = self._make_revealed_cycle(project)
        CycleParticipation.objects.create(cycle=cycle, member=member)
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "1 of 2 submitted" in content
        assert member.username in content

    def test_publish_form_defaults_to_meeting_record_extracted_summary(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        cycle = self._make_revealed_cycle(project)
        MeetingRecord.objects.create(
            cycle=cycle,
            kind=MeetingRecord.Kind.PASTED_TEXT,
            transcript_text="...",
            processing_state=MeetingRecord.ProcessingState.COMPLETED,
            extracted_summary="The team discussed deploy pains.",
        )
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "The team discussed deploy pains." in content


# -- Card anonymity is preserved, exactly as on the board (#10) --


class TestCycleSummaryCardAnonymity(SummaryTestBase):
    def test_anonymous_card_shows_no_author(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_revealed_cycle(project)
        Card.objects.create(
            cycle=cycle,
            category=Card.Category.START,
            text="Secret feedback",
            author=None,
            edit_token_hash="deadbeef",
        )
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Secret feedback" in content
        assert member.username not in content
        assert facilitator.username not in content
        assert "Anonymous" in content

    def test_attributed_card_shows_authors_username(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle = self._make_revealed_cycle(project)
        Card.objects.create(
            cycle=cycle, category=Card.Category.START, text="Attributed feedback", author=member
        )
        client.force_login(facilitator)

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Attributed feedback" in content
        assert member.username in content


# -- Publishing: facilitator-only, one-shot --


class TestPublishSummary(SummaryTestBase):
    def test_facilitator_can_publish(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "30"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)
        before = timezone.now()

        response = client.post(self._publish_url(project, cycle), {"body": "Went well."})

        assert response.status_code == 302
        assert response.url == self._summary_url(project, cycle)
        summary = RetrospectiveSummary.objects.get(cycle=cycle)
        assert summary.body == "Went well."
        assert summary.published_at is not None
        assert summary.published_at >= before

    def test_publishing_sets_completed_at_and_closes_the_cycle(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "31"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)
        before = timezone.now()

        client.post(self._publish_url(project, cycle))

        cycle.refresh_from_db()
        assert cycle.completed_at is not None
        assert cycle.completed_at >= before
        assert cycle.state == FeedbackCycle.State.CLOSED

    def test_body_is_optional(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "32"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.post(self._publish_url(project, cycle))

        assert response.status_code == 302
        summary = RetrospectiveSummary.objects.get(cycle=cycle)
        assert summary.body == ""

    def test_member_cannot_publish_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "33"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(member)

        response = client.post(self._publish_url(project, cycle))

        assert response.status_code == 404
        assert not RetrospectiveSummary.objects.filter(cycle=cycle).exists()
        cycle.refresh_from_db()
        assert cycle.completed_at is None
        assert cycle.state != FeedbackCycle.State.CLOSED

    def test_anonymous_post_is_redirected_to_login_and_publishes_nothing(
        self, client, django_user_model
    ):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "34"
        )
        cycle = self._make_revealed_cycle(project)

        response = client.post(self._publish_url(project, cycle))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))
        assert not RetrospectiveSummary.objects.filter(cycle=cycle).exists()

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "35"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._publish_url(project, cycle))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_publish", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(
            reverse("publish_summary", kwargs={"pk": 999999, "cycle_id": 1})
        )

        assert response.status_code == 404

    def test_cycle_from_another_project_cannot_be_published(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "36"
        )
        _other_facilitator, _other_member, other_project = (
            self._make_project_with_facilitator_and_member(django_user_model, "36b")
        )
        other_cycle = self._make_revealed_cycle(other_project)
        client.force_login(facilitator)

        response = client.post(self._publish_url(project, other_cycle))

        assert response.status_code == 404
        assert not RetrospectiveSummary.objects.filter(cycle=other_cycle).exists()


# -- Publishing is rejected if a summary already exists for the cycle --


class TestPublishSummaryIsOneShot(SummaryTestBase):
    def test_publishing_twice_does_not_create_a_second_summary(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "40"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        first_response = client.post(
            self._publish_url(project, cycle), {"body": "First publish."}
        )
        assert first_response.status_code == 302
        first_summary = RetrospectiveSummary.objects.get(cycle=cycle)

        second_response = client.post(
            self._publish_url(project, cycle), {"body": "Second attempt."}
        )

        assert second_response.status_code == 302
        assert RetrospectiveSummary.objects.filter(cycle=cycle).count() == 1
        first_summary.refresh_from_db()
        assert first_summary.body == "First publish."

    def test_db_constraint_prevents_two_summaries_for_one_cycle(
        self, django_user_model
    ):
        # Exercises the OneToOneField's uniqueness directly at the ORM/DB
        # layer, bypassing the view's own check-first logic entirely — the
        # same "DB is the real guarantee" reasoning
        # test_projects.TestCreateCycle.
        # test_db_constraint_prevents_two_active_cycles_regardless_of_app_logic
        # applies to unique_active_cycle_per_project.
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "41"
        )
        cycle = self._make_revealed_cycle(project)
        RetrospectiveSummary.objects.create(cycle=cycle, body="First.")

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                RetrospectiveSummary.objects.create(cycle=cycle, body="Second.")

        assert RetrospectiveSummary.objects.filter(cycle=cycle).count() == 1


# -- The issue's own explicitly required test: an unconfirmed decision/
# action item never appears in the published summary; a confirmed one does.


class TestPublishedSummaryOnlyShowsConfirmedRows(SummaryTestBase):
    def test_unconfirmed_decision_is_excluded_confirmed_one_is_shown(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "50"
        )
        cycle = self._make_revealed_cycle(project)
        DecisionDraft.objects.create(
            cycle=cycle, text="Unconfirmed decision text.", source=DraftSource.AI
        )
        DecisionDraft.objects.create(
            cycle=cycle,
            text="Confirmed decision text.",
            source=DraftSource.AI,
            confirmed_at=timezone.now(),
            confirmed_by=facilitator,
        )
        client.force_login(facilitator)
        client.post(self._publish_url(project, cycle))

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Confirmed decision text." in content
        assert "Unconfirmed decision text." not in content

    def test_unconfirmed_action_item_is_excluded_confirmed_one_is_shown(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "51"
        )
        cycle = self._make_revealed_cycle(project)
        ActionItem.objects.create(
            cycle=cycle, description="Unconfirmed action item.", source=DraftSource.AI
        )
        ActionItem.objects.create(
            cycle=cycle,
            description="Confirmed action item.",
            source=DraftSource.AI,
            confirmed_at=timezone.now(),
            confirmed_by=facilitator,
        )
        client.force_login(facilitator)
        client.post(self._publish_url(project, cycle))

        response = client.get(self._summary_url(project, cycle))
        content = response.content.decode()

        assert "Confirmed action item." in content
        assert "Unconfirmed action item." not in content

    def test_unconfirmed_rows_stay_excluded_from_a_member_view_too(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "52"
        )
        cycle = self._make_revealed_cycle(project)
        DecisionDraft.objects.create(
            cycle=cycle, text="Still a draft.", source=DraftSource.AI
        )
        client.force_login(facilitator)
        client.post(self._publish_url(project, cycle))

        client.force_login(member)
        response = client.get(self._summary_url(project, cycle))

        assert "Still a draft." not in response.content.decode()


# -- The issue's other explicitly required test: after publishing, #7's
# create-cycle action succeeds again for the same project.


class TestCreateCycleAfterPublishing(SummaryTestBase):
    def test_create_cycle_succeeds_again_after_publishing(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "60"
        )
        cycle = self._make_revealed_cycle(project)
        client.force_login(facilitator)

        # Before publishing: a second cycle is rejected (#7's rule, still
        # in force while this cycle is active).
        blocked_response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))
        assert blocked_response.status_code == 302
        assert FeedbackCycle.objects.filter(project=project).count() == 1

        publish_response = client.post(self._publish_url(project, cycle))
        assert publish_response.status_code == 302
        cycle.refresh_from_db()
        assert cycle.state == FeedbackCycle.State.CLOSED

        create_response = client.post(reverse("create_cycle", kwargs={"pk": project.pk}))

        assert create_response.status_code == 302
        assert FeedbackCycle.objects.filter(project=project).count() == 2
        new_cycle = (
            FeedbackCycle.objects.filter(project=project)
            .exclude(pk=cycle.pk)
            .get()
        )
        assert new_cycle.state == FeedbackCycle.State.COLLECTING
        assert new_cycle.week == "Cycle 2"

    def test_db_constraint_is_satisfied_by_the_closed_state_not_completed_at_alone(
        self, django_user_model
    ):
        # Directly proves the mechanism the acceptance criteria points at:
        # unique_active_cycle_per_project is keyed on state != closed, not
        # on completed_at. A cycle with completed_at set but state left
        # non-closed would still block a second cycle; publish_summary sets
        # both together specifically so this constraint clears.
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "61"
        )
        FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.DISCUSSING,
            completed_at=timezone.now(),
        )

        with pytest.raises(IntegrityError):
            with transaction.atomic():
                FeedbackCycle.objects.create(
                    project=project, week="Cycle 2", state=FeedbackCycle.State.COLLECTING
                )
