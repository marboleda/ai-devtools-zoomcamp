"""URLconf used only by tests that need a login-required view to hit.

No such page exists in the product yet (per #2's acceptance criteria: "once
any exist"), so this stands in for one.
"""
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.urls import include, path

from config.views import home


@login_required
def protected(request):
    return HttpResponse("protected")


urlpatterns = [
    path("", home, name="home"),
    path("protected/", protected, name="protected"),
    path("accounts/", include("accounts.urls")),
]
