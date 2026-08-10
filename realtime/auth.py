"""Authentication for technical JSON and fragment endpoints.

`django.contrib.auth.decorators.login_required` is right for a page a human
navigates to: it redirects to the login form. A JSON or fragment endpoint
consumed by `fetch()` must never receive that redirect — the browser would
resolve it into an HTML document that the client cannot and must not try to
parse as JSON, and an expired session would otherwise look like a silent
failure instead of a clear signal to stop.

This decorator gives every technical endpoint one uniform, unauthenticated-safe
answer instead. It carries no model or object permission of its own: any
resource-level access rule stays the responsibility of each view.
"""

import functools

from django.http import JsonResponse


def realtime_login_required(view):
    """Require an authenticated session; answer 401 JSON instead of redirecting."""

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            response = JsonResponse({'error': 'authentication_required'}, status=401)
            response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
            response['Vary'] = 'Cookie'
            return response
        return view(request, *args, **kwargs)

    return wrapper
