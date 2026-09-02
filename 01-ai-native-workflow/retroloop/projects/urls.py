from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.create_project, name="create_project"),
    path("<int:pk>/", views.project_detail, name="project_detail"),
    path("join/", views.join_project, name="join_project"),
]
