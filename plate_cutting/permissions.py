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
    return bool(
        profile.pk
        and profile.is_active
        and profile.role in PRESET_MANAGING_ROLES
    )
