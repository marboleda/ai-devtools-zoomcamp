from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.create_project, name="create_project"),
    path("<int:pk>/", views.project_detail, name="project_detail"),
    path("<int:pk>/cycles/new/", views.create_cycle, name="create_cycle"),
    path("join/", views.join_project, name="join_project"),
]
