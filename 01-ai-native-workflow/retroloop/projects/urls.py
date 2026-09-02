from django.urls import path

from . import views

urlpatterns = [
    path("new/", views.create_project, name="create_project"),
    path("<int:pk>/created/", views.project_created, name="project_created"),
]
