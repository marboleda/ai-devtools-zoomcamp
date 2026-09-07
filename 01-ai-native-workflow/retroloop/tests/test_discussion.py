"""Tests for #18: discussion mode — the board's fourth mode, reachable once
#17's close_voting has produced the DiscussionTopic agenda.

Two different permission levels apply on the same page: only the
facilitator (#6's check) can set a topic's outcome to discussed / skipped /
deferred, but *any* member can attach a free-text note to whichever topic
is currently under discussion — per #18's decision, whichever
DiscussionTopic has no outcome yet and the lowest rank, computed at query
time (DiscussionTopic.objects.current_for_cycle), since no model carries a
separate "current topic" pointer field.

Mirrors tests/test_close_voting.py and tests/test_voting.py for fixture and
naming conventions.
"""
import pytest
from django.urls import reverse
from django.utils import timezone

from projects.models import (
    Cluster,
    DiscussionTopic,
    FeedbackCycle,
    Membership,
    Project,
    Vote,
)

VALID_PASSWORD = "correct horse battery staple"


@pytest.mark.django_db
class DiscussionTestBase:
    def _make_project_with_facilitator_and_member(self, django_user_model, suffix=""):
        facilitator = django_user_model.objects.create_user(
            username=f"dfac{suffix}", password=VALID_PASSWORD
        )
        project = Project.objects.create(
            name=f"Discussion Project {suffix}", created_by=facilitator
        )
        Membership.objects.create(
            project=project, user=facilitator, role=Membership.Role.FACILITATOR
        )
        member = django_user_model.objects.create_user(
            username=f"dmem{suffix}", password=VALID_PASSWORD
        )
        Membership.objects.create(project=project, user=member, role=Membership.Role.MEMBER)
        return facilitator, member, project

    def _make_revealed_cycle(self, project, voting_closed=False):
        return FeedbackCycle.objects.create(
            project=project,
            week="Cycle 1",
            state=FeedbackCycle.State.DISCUSSING if voting_closed else FeedbackCycle.State.VOTING,
            revealed_at=timezone.now(),
            voting_closed_at=timezone.now() if voting_closed else None,
        )

    def _make_collecting_cycle(self, project):
        return FeedbackCycle.objects.create(
            project=project, week="Cycle 1", state=FeedbackCycle.State.COLLECTING
        )

    def _make_topic(self, cycle, name, rank, outcome=""):
        cluster = Cluster.objects.create(
            cycle=cycle, name=name, origin=Cluster.Origin.SUGGESTED
        )
        return DiscussionTopic.objects.create(cluster=cluster, rank=rank, outcome=outcome)

    def _board_discuss_url(self, project):
        return reverse("board_discuss", kwargs={"pk": project.pk})

    def _set_outcome_url(self, project, topic):
        return reverse(
            "set_topic_outcome", kwargs={"pk": project.pk, "topic_id": topic.pk}
        )

    def _add_note_url(self, project):
        return reverse("add_discussion_note", kwargs={"pk": project.pk})


# -- Model/manager layer: current_for_cycle and for_cycle, called directly
# (not through a view), the same way #16 tests VoteQuerySet methods.


class TestDiscussionTopicQuerySet(DiscussionTestBase):
    def test_current_for_cycle_returns_the_lowest_ranked_blank_outcome_topic(
        self, django_user_model
    ):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "1"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        first = self._make_topic(cycle, "First", rank=1, outcome=DiscussionTopic.Outcome.DISCUSSED)
        second = self._make_topic(cycle, "Second", rank=2)
        self._make_topic(cycle, "Third", rank=3)

        current = DiscussionTopic.objects.current_for_cycle(cycle=cycle)

        assert current == second

    def test_current_for_cycle_is_none_once_every_topic_has_an_outcome(
        self, django_user_model
    ):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "2"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "First", rank=1, outcome=DiscussionTopic.Outcome.DISCUSSED)
        self._make_topic(cycle, "Second", rank=2, outcome=DiscussionTopic.Outcome.SKIPPED)

        assert DiscussionTopic.objects.current_for_cycle(cycle=cycle) is None

    def test_current_for_cycle_is_none_with_no_topics_at_all(self, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "3"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)

        assert DiscussionTopic.objects.current_for_cycle(cycle=cycle) is None

    def test_for_cycle_returns_only_topics_in_that_cycle_in_rank_order(
        self, django_user_model
    ):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "4"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        third = self._make_topic(cycle, "Third", rank=3)
        first = self._make_topic(cycle, "First", rank=1)
        second = self._make_topic(cycle, "Second", rank=2)

        other_owner = django_user_model.objects.create_user(
            username="other_owner_dq4", password=VALID_PASSWORD
        )
        other_project = Project.objects.create(name="Other", created_by=other_owner)
        other_cycle = self._make_revealed_cycle(other_project, voting_closed=True)
        self._make_topic(other_cycle, "Not yours", rank=1)

        topics = list(DiscussionTopic.objects.for_cycle(cycle=cycle))

        assert topics == [first, second, third]


# -- board_discuss view: reachability, the four readiness states, and
# listing topics in rank order.


class TestBoardDiscussView(DiscussionTestBase):
    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "5"
        )
        outsider = django_user_model.objects.create_user(
            username="outsider_bd1", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.get(self._board_discuss_url(project))

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "6"
        )

        response = client.get(self._board_discuss_url(project))

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_bd", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.get(reverse("board_discuss", kwargs={"pk": 999999}))

        assert response.status_code == 404

    def test_no_active_cycle_shows_friendly_message(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "7"
        )
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "No active feedback cycle yet." in content

    def test_before_reveal_shows_not_revealed_message(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "8"
        )
        self._make_collecting_cycle(project)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "haven't been revealed" in content

    def test_after_reveal_before_voting_closed_shows_friendly_message(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "9"
        )
        self._make_revealed_cycle(project, voting_closed=False)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Voting hasn't closed yet." in content

    def test_ordinary_member_not_just_facilitator_can_view(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "10"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Theme A", rank=1)
        client.force_login(member)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert response.status_code == 200
        assert "Theme A" in content

    def test_topics_are_listed_in_rank_order(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "11"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        # Created out of rank order, on purpose.
        self._make_topic(cycle, "Third topic", rank=3)
        self._make_topic(cycle, "First topic", rank=1)
        self._make_topic(cycle, "Second topic", rank=2)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        first_index = content.index("First topic")
        second_index = content.index("Second topic")
        third_index = content.index("Third topic")
        assert first_index < second_index < third_index

    def test_htmx_request_returns_only_the_fragment_not_the_full_page(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "12"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Polled topic", rank=1)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project), HTTP_HX_REQUEST="true")
        content = response.content.decode()

        assert response.status_code == 200
        assert "Polled topic" in content
        assert "htmx.org" not in content
        assert "Back to project" not in content

    def test_non_htmx_request_returns_the_full_page(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "13"
        )
        self._make_revealed_cycle(project, voting_closed=True)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert "htmx.org" in content
        assert "Back to project" in content

    def test_page_carries_htmx_polling_markup(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "14"
        )
        self._make_revealed_cycle(project, voting_closed=True)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert 'hx-trigger="every 3s"' in content
        assert f'hx-get="{self._board_discuss_url(project)}"' in content

    def test_board_discuss_link_reachable_from_board_vote(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "15"
        )
        self._make_revealed_cycle(project, voting_closed=True)
        client.force_login(facilitator)

        response = client.get(reverse("board_vote", kwargs={"pk": project.pk}))
        content = response.content.decode()

        assert self._board_discuss_url(project) in content

    def test_resolved_topic_shows_its_outcome_not_the_discussion_controls(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "16"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(
            cycle, "Already discussed", rank=1, outcome=DiscussionTopic.Outcome.DISCUSSED
        )
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert "Outcome: Discussed" in content
        assert "Currently under discussion" not in content

    def test_future_topic_shows_not_yet_discussed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "17"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Current", rank=1)
        self._make_topic(cycle, "Later", rank=2)
        client.force_login(facilitator)

        response = client.get(self._board_discuss_url(project))
        content = response.content.decode()

        assert "Not yet discussed." in content


# -- set_topic_outcome: facilitator-only (per #6), the explicitly required
# test that a non-facilitator member cannot set an outcome.


class TestSetTopicOutcome(DiscussionTestBase):
    def test_facilitator_can_mark_a_topic_discussed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "18"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )

        assert response.status_code == 200
        topic.refresh_from_db()
        assert topic.outcome == DiscussionTopic.Outcome.DISCUSSED

    def test_facilitator_can_mark_a_topic_skipped(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "19"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        client.post(self._set_outcome_url(project, topic), {"outcome": "skipped"})

        topic.refresh_from_db()
        assert topic.outcome == DiscussionTopic.Outcome.SKIPPED

    def test_facilitator_can_mark_a_topic_deferred(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "20"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        client.post(self._set_outcome_url(project, topic), {"outcome": "deferred"})

        topic.refresh_from_db()
        assert topic.outcome == DiscussionTopic.Outcome.DEFERRED

    def test_member_cannot_set_outcome_and_gets_404(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "21"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(member)

        response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )

        assert response.status_code == 404
        topic.refresh_from_db()
        assert topic.outcome == ""

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "22"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)

        response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "23"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        response = client.get(self._set_outcome_url(project, topic))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_sto", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(
            reverse(
                "set_topic_outcome", kwargs={"pk": 999999, "topic_id": 1}
            ),
            {"outcome": "discussed"},
        )

        assert response.status_code == 404

    def test_invalid_outcome_value_gets_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "24"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "not-a-real-outcome"}
        )

        assert response.status_code == 404
        topic.refresh_from_db()
        assert topic.outcome == ""

    def test_before_voting_closed_gets_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "25"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=False)
        cluster = Cluster.objects.create(
            cycle=cycle, name="Theme", origin=Cluster.Origin.SUGGESTED
        )
        # Not reachable via the normal flow (no DiscussionTopic exists
        # before close_voting), but exercised directly against a topic
        # attached to a cycle whose voting isn't closed, to prove the
        # guard is on cycle state, not merely "does the topic exist".
        topic = DiscussionTopic.objects.create(cluster=cluster, rank=1)
        client.force_login(facilitator)

        response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )

        assert response.status_code == 404
        topic.refresh_from_db()
        assert topic.outcome == ""

    def test_topic_from_another_project_gets_404(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "26"
        )
        self._make_revealed_cycle(project, voting_closed=True)

        other_owner = django_user_model.objects.create_user(
            username="other_owner_sto", password=VALID_PASSWORD
        )
        other_project = Project.objects.create(name="Other", created_by=other_owner)
        other_cycle = self._make_revealed_cycle(other_project, voting_closed=True)
        foreign_topic = self._make_topic(other_cycle, "Not yours", rank=1)
        client.force_login(facilitator)

        response = client.post(
            self._set_outcome_url(project, foreign_topic), {"outcome": "discussed"}
        )

        assert response.status_code == 404
        foreign_topic.refresh_from_db()
        assert foreign_topic.outcome == ""

    def test_setting_outcome_advances_which_topic_is_current(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "27"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        first = self._make_topic(cycle, "First", rank=1)
        second = self._make_topic(cycle, "Second", rank=2)
        client.force_login(facilitator)

        assert DiscussionTopic.objects.current_for_cycle(cycle=cycle) == first

        client.post(self._set_outcome_url(project, first), {"outcome": "discussed"})

        assert DiscussionTopic.objects.current_for_cycle(cycle=cycle) == second


# -- add_discussion_note: any member, always targets the current topic —
# the explicitly required test that a non-facilitator member can add a
# note (while it cannot set an outcome).


class TestAddDiscussionNote(DiscussionTestBase):
    def test_non_facilitator_member_cannot_set_outcome_but_can_add_a_note(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "28"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(member)

        outcome_response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )
        note_response = client.post(
            self._add_note_url(project), {"text": "We agreed to try X next week."}
        )

        assert outcome_response.status_code == 404
        topic.refresh_from_db()
        assert topic.outcome == ""
        assert note_response.status_code == 200
        assert "We agreed to try X next week." in topic.notes

    def test_facilitator_can_both_set_outcome_and_add_a_note(
        self, client, django_user_model
    ):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "29"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        note_response = client.post(
            self._add_note_url(project), {"text": "Facilitator's note."}
        )
        outcome_response = client.post(
            self._set_outcome_url(project, topic), {"outcome": "discussed"}
        )

        assert note_response.status_code == 200
        assert outcome_response.status_code == 200
        topic.refresh_from_db()
        assert "Facilitator's note." in topic.notes
        assert topic.outcome == DiscussionTopic.Outcome.DISCUSSED

    def test_note_is_attributed_to_its_author(self, client, django_user_model):
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "30"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(member)

        client.post(self._add_note_url(project), {"text": "My observation."})

        topic.refresh_from_db()
        assert f"{member.username}: My observation." in topic.notes

    def test_multiple_notes_from_different_members_both_accumulate(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "31"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)

        client.force_login(member)
        client.post(self._add_note_url(project), {"text": "First note."})
        client.force_login(facilitator)
        client.post(self._add_note_url(project), {"text": "Second note."})

        topic.refresh_from_db()
        assert "First note." in topic.notes
        assert "Second note." in topic.notes
        # Both preserved, not the second overwriting the first.
        assert topic.notes.index("First note.") < topic.notes.index("Second note.")

    def test_notes_never_apply_to_a_topic_that_already_has_an_outcome(
        self, client, django_user_model
    ):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "32"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        resolved = self._make_topic(
            cycle, "Resolved", rank=1, outcome=DiscussionTopic.Outcome.DISCUSSED
        )
        current = self._make_topic(cycle, "Current", rank=2)
        client.force_login(member)

        client.post(self._add_note_url(project), {"text": "A note."})

        resolved.refresh_from_db()
        current.refresh_from_db()
        assert resolved.notes == ""
        assert "A note." in current.notes

    def test_no_current_topic_gets_404(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "33"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Resolved", rank=1, outcome=DiscussionTopic.Outcome.SKIPPED)
        client.force_login(member)

        response = client.post(self._add_note_url(project), {"text": "A note."})

        assert response.status_code == 404

    def test_no_topics_at_all_gets_404(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "34"
        )
        self._make_revealed_cycle(project, voting_closed=True)
        client.force_login(member)

        response = client.post(self._add_note_url(project), {"text": "A note."})

        assert response.status_code == 404

    def test_before_voting_closed_gets_404(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "35"
        )
        self._make_revealed_cycle(project, voting_closed=False)
        client.force_login(member)

        response = client.post(self._add_note_url(project), {"text": "A note."})

        assert response.status_code == 404

    def test_non_member_gets_404(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "36"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Theme", rank=1)
        outsider = django_user_model.objects.create_user(
            username="outsider_adn", password=VALID_PASSWORD
        )
        client.force_login(outsider)

        response = client.post(self._add_note_url(project), {"text": "A note."})

        assert response.status_code == 404

    def test_anonymous_visitor_is_redirected_to_login(self, client, django_user_model):
        _facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "37"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Theme", rank=1)

        response = client.post(self._add_note_url(project), {"text": "A note."})

        assert response.status_code == 302
        assert response.url.startswith(reverse("login"))

    def test_get_request_is_not_allowed(self, client, django_user_model):
        facilitator, _member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "38"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        self._make_topic(cycle, "Theme", rank=1)
        client.force_login(facilitator)

        response = client.get(self._add_note_url(project))

        assert response.status_code == 405

    def test_nonexistent_project_gets_404(self, client, django_user_model):
        user = django_user_model.objects.create_user(
            username="ghost_adn", password=VALID_PASSWORD
        )
        client.force_login(user)

        response = client.post(
            reverse("add_discussion_note", kwargs={"pk": 999999}), {"text": "A note."}
        )

        assert response.status_code == 404

    def test_blank_note_is_rejected_without_crashing(self, client, django_user_model):
        facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "39"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(member)

        response = client.post(self._add_note_url(project), {"text": "   "})

        assert response.status_code == 200
        topic.refresh_from_db()
        assert topic.notes == ""

    def test_no_ai_call_note_is_stored_as_plain_text_verbatim(
        self, client, django_user_model
    ):
        # Sanity check for #18's "no AI involvement" constraint: nothing in
        # the note-adding path imports or calls the Anthropic SDK. There is
        # no client/function to stub out here because none exists — this
        # test instead asserts the stored text is exactly what was typed,
        # with only the author-attribution prefix added, i.e. nothing has
        # summarized, rewritten, or extracted anything from it.
        _facilitator, member, project = self._make_project_with_facilitator_and_member(
            django_user_model, "40"
        )
        cycle = self._make_revealed_cycle(project, voting_closed=True)
        topic = self._make_topic(cycle, "Theme", rank=1)
        client.force_login(member)
        raw_text = "Decision: we will pair on this. Action: Alice to follow up."

        client.post(self._add_note_url(project), {"text": raw_text})

        topic.refresh_from_db()
        assert topic.notes == f"{member.username}: {raw_text}"
