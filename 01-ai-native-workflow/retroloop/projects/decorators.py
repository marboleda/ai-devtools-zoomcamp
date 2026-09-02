from functools import wraps

from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404

from .models import Membership


def facilitator_required(view_func):
    """Gate a view behind Membership.role == facilitator for the project
    named in the URL.

    Wraps a view function whose URL captures the project's primary key as
    ``pk`` (the ``<int:pk>/`` convention already used by ``project_detail``).

    Calling convention:

    - The decorator handles the signed-out case itself, the same way
      ``@login_required`` does elsewhere in the app (in fact it wraps this
      decorator around the view too) — an anonymous request is redirected
      to login. The view does not need to also be stacked with
      ``@login_required``.
    - The decorator does its own ``Membership`` lookup for
      ``(project=pk, user=request.user, role=facilitator)``. If no such
      membership exists — no membership at all, or a membership with
      ``role=member`` — the request gets a 404. This is deliberately the
      *same* 404 as "project doesn't exist", via a single query, so a
      wrong-role or non-member request never confirms the project's
      existence or structure (matching #5's ``project_detail`` behaviour).
    - On success, the wrapped view is called with the request and all of
      the URL's original positional/keyword arguments (including ``pk``,
      unchanged) *plus* two extra keyword arguments the view can rely on
      instead of re-querying: ``project`` (the resolved ``Project``) and
      ``membership`` (the resolved ``Membership``, whose role is already
      known to be facilitator).

    A view protected by ``@facilitator_required`` alone is fully gated: it
    carries no permission logic of its own.
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        membership = get_object_or_404(
            Membership.objects.select_related("project"),
            project_id=kwargs.get("pk"),
            user=request.user,
            role=Membership.Role.FACILITATOR,
        )
        return view_func(
            request, *args, project=membership.project, membership=membership, **kwargs
        )

    return login_required(wrapper)
