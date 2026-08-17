"""Who owns the «Проработка» journal.

One rule, one function, one place. Reading the journal — the Calculator tab,
the entry list, the search and the `.xlsx` export — stays open to every
authenticated user, exactly as before. *Changing* it belongs to the planning
and dispatch office and to the administrator, and that is expressed as the
`UserProfile.Role.PDO` and `UserProfile.Role.ADMIN` roles, never as
department membership: the department is an organisational fact that an
administrator may attach to anyone, while the role is the deliberate grant.

`is_staff` and the username stay irrelevant here. Genuine `is_superuser` is
the one profile-independent fallback, exactly as `acts.permissions
.is_act_admin()` already treats it, so a system owner is never locked out of
the journal by a missing or deactivated profile.
"""
from accounts.models import UserProfile

MANAGING_ROLES = frozenset({UserProfile.Role.PDO, UserProfile.Role.ADMIN})


def can_manage_workup(user):
    """Return whether `user` may create, edit, confirm or delete journal rows."""
    if not getattr(user, 'is_authenticated', False):
        return False
    # The administrative fallback is independent of the profile, like acts.
    if getattr(user, 'is_superuser', False):
        return True
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return False
    # An inactive profile grants no application role anywhere in this project.
    if profile.pk is None or not profile.is_active:
        return False
    return profile.role in MANAGING_ROLES
