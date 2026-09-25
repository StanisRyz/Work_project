"""Permissions for the shared plate-cutting preset library."""
from accounts.models import UserProfile


PRESET_MANAGING_ROLES = frozenset({
    UserProfile.Role.PDO,
    UserProfile.Role.ADMIN,
})


def can_manage_plate_cutting_presets(user):
    """Return whether ``user`` may create, overwrite or delete presets."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return False
    if not profile.pk or not profile.is_active:
        return False
    # The profile's role or one lent by a substitution in force today.
    from accounts.roles import has_any_role

    return has_any_role(user, PRESET_MANAGING_ROLES)
