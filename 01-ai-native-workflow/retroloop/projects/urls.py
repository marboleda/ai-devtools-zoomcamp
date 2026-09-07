from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.create_project, name="create_project"),
    path("<int:pk>/", views.project_detail, name="project_detail"),
    path("<int:pk>/cycles/new/", views.create_cycle, name="create_cycle"),
    path("<int:pk>/cycles/reveal/", views.reveal_cycle, name="reveal_cycle"),
    path("<int:pk>/board/reveal/", views.board_reveal, name="board_reveal"),
    path("<int:pk>/board/cluster/", views.board_cluster, name="board_cluster"),
    path(
        "<int:pk>/board/cluster/cards/<int:card_id>/move/",
        views.move_card,
        name="move_card",
    ),
    path(
        "<int:pk>/board/cluster/clusters/<int:cluster_id>/rename/",
        views.rename_cluster,
        name="rename_cluster",
    ),
    path(
        "<int:pk>/board/cluster/clusters/<int:cluster_id>/merge/",
        views.merge_clusters,
        name="merge_clusters",
    ),
    path(
        "<int:pk>/board/cluster/clusters/<int:cluster_id>/split/",
        views.split_cluster,
        name="split_cluster",
    ),
    path("<int:pk>/board/vote/", views.board_vote, name="board_vote"),
    path(
        "<int:pk>/board/vote/clusters/<int:cluster_id>/cast/",
        views.cast_vote,
        name="cast_vote",
    ),
    path(
        "<int:pk>/cycles/close-voting/", views.close_voting, name="close_voting"
    ),
    path("<int:pk>/board/discuss/", views.board_discuss, name="board_discuss"),
    path(
        "<int:pk>/board/discuss/topics/<int:topic_id>/outcome/",
        views.set_topic_outcome,
        name="set_topic_outcome",
    ),
    path(
        "<int:pk>/board/discuss/note/",
        views.add_discussion_note,
        name="add_discussion_note",
    ),
    path("<int:pk>/cards/new/", views.create_card, name="create_card"),
    path("<int:pk>/cards/<int:card_id>/edit/", views.edit_card, name="edit_card"),
    path(
        "<int:pk>/cards/<int:card_id>/withdraw/",
        views.withdraw_card,
        name="withdraw_card",
    ),
    path("<int:pk>/meeting/upload/", views.meeting_upload, name="meeting_upload"),
    path(
        "<int:pk>/meeting/upload/status/",
        views.meeting_upload_status,
        name="meeting_upload_status",
    ),
    path("<int:pk>/review/", views.review_drafts, name="review_drafts"),
    path(
        "<int:pk>/review/decisions/new/",
        views.create_decision_draft,
        name="create_decision_draft",
    ),
    path(
        "<int:pk>/review/decisions/<int:draft_id>/confirm/",
        views.confirm_decision_draft,
        name="confirm_decision_draft",
    ),
    path(
        "<int:pk>/review/decisions/<int:draft_id>/discard/",
        views.discard_decision_draft,
        name="discard_decision_draft",
    ),
    path(
        "<int:pk>/review/actions/new/",
        views.create_action_item,
        name="create_action_item",
    ),
    path(
        "<int:pk>/review/actions/<int:item_id>/confirm/",
        views.confirm_action_item,
        name="confirm_action_item",
    ),
    path(
        "<int:pk>/review/actions/<int:item_id>/discard/",
        views.discard_action_item,
        name="discard_action_item",
    ),
    path(
        "<int:pk>/cycles/<int:cycle_id>/summary/",
        views.cycle_summary,
        name="cycle_summary",
    ),
    path(
        "<int:pk>/cycles/<int:cycle_id>/summary/publish/",
        views.publish_summary,
        name="publish_summary",
    ),
    path("join/", views.join_project, name="join_project"),
]
