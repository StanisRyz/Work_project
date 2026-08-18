"""Temporary compatibility for the public paths used before the URL hierarchy.

The modules moved under `/quality/<module>/` and `/calculators/<module>/`; the
old flat paths keep working so bookmarks, open tabs and pasted links survive
the move. Nothing is duplicated: a legacy request is answered with a redirect
to the canonical location, and only for paths that actually exist there — a
legacy URL for a route that is not registered stays an ordinary 404, exactly
as it was before the move.

The redirect is 307 rather than 301/302 on purpose. A state-changing POST that
arrived at a legacy path must reach the canonical view as a POST; 301 and 302
are allowed to turn it into a GET. 307 is also temporary, so browsers do not
cache the alias after these routes are eventually removed.

The patterns below are deliberately unnamed, so `reverse()` and `{% url %}`
can never produce a legacy path — Django-generated links are always canonical.
"""

from django.http import Http404
from django.http.response import HttpResponseRedirectBase
from django.urls import Resolver404, resolve


class HttpResponseTemporaryRedirect(HttpResponseRedirectBase):
    """A redirect the client must repeat with the original method and body."""

    status_code = 307


def legacy_prefix_alias(canonical_prefix):
    """A view redirecting `<legacy prefix>/<rest>` to `<canonical prefix>/<rest>`."""

    def view(request, rest=''):
        target = f'{canonical_prefix}{rest}'
        try:
            resolve(target, urlconf=getattr(request, 'urlconf', None))
        except Resolver404:
            raise Http404(f'No canonical route for the legacy path {request.path}')
        query = request.META.get('QUERY_STRING')
        return HttpResponseTemporaryRedirect(f'{target}?{query}' if query else target)

    return view
