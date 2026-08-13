"""Where an authenticated user belongs after login and at the application root.

`/acts/` is the working page for everyone; only administrators and genuine
Django superusers keep the dashboard landing page. Reused by the login view,
the dashboard root and the navigation templates so the answer lives in one
place.
"""
from django.urls import reverse

from acts.permissions import is_act_admin


def is_admin_user(user):
    """Administrator role or the Django superuser fallback."""
    return is_act_admin(user)


def get_default_landing_url(user):
    """The page an authenticated user is sent to when no explicit target exists."""
    return reverse('dashboard:home') if is_admin_user(user) else reverse('acts:list')
