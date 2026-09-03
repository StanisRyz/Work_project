"""Where an authenticated user belongs after login and at the application root.

The dashboard at `/` is the working entry point for everyone, including
administrators and Django superusers: it carries the quick access grid and the
user's own open tasks. Reused by the login view and the navigation templates so
the answer lives in one place.
"""
from django.urls import reverse


def get_default_landing_url(user):
    """The page an authenticated user is sent to when no explicit target exists."""
    return reverse('dashboard:home')
