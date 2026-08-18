"""Where an authenticated user belongs after login and at the application root.

`/quality/acts/` is the working page for everyone, including administrators and
Django superusers. Reused by the login view and the navigation templates so
the answer lives in one place.
"""
from django.urls import reverse


def get_default_landing_url(user):
    """The page an authenticated user is sent to when no explicit target exists."""
    return reverse('acts:list')
