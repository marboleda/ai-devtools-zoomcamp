from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.create_project, name="create_project"),
    path("<int:pk>/", views.project_detail, name="project_detail"),
    path("<int:pk>/cycles/new/", views.create_cycle, name="create_cycle"),
    path("<int:pk>/cycles/reveal/", views.reveal_cycle, name="reveal_cycle"),
    path("<int:pk>/board/reveal/", views.board_reveal, name="board_reveal"),
    path("<int:pk>/cards/new/", views.create_card, name="create_card"),
    path("<int:pk>/cards/<int:card_id>/edit/", views.edit_card, name="edit_card"),
    path(
        "<int:pk>/cards/<int:card_id>/withdraw/",
        views.withdraw_card,
        name="withdraw_card",
    ),
    path("join/", views.join_project, name="join_project"),
]
