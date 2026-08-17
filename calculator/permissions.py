"""Who owns the «Проработка» journal.

One rule, one function, one place. Reading the journal — the Calculator tab,
the entry list, the search and the `.xlsx` export — stays open to every
authenticated user, exactly as before. *Changing* it belongs to the planning
and dispatch office, and that is expressed as the `UserProfile.Role.PDO`
role, never as department membership: the department is an organisational
fact that an administrator may attach to anyone, while the role is the
deliberate grant.

`is_staff`, `is_superuser` and the username are equally irrelevant here. A
superuser who has to work the journal gets the role like everyone else, so
there is exactly one answer to «may this user change Проработка?».
"""
from accounts.models import UserProfile


def can_manage_workup(user):
    """Return whether `user` may create, edit, confirm or delete journal rows."""
    if not getattr(user, 'is_authenticated', False):
        return False
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return False
    # An inactive profile grants no application role anywhere in this project.
    if profile.pk is None or not profile.is_active:
        return False
    return profile.role == UserProfile.Role.PDO
