"""Who may read and change a protocol.

Deliberately small: there is no Protocols UI yet, so this file answers only
the three questions the next stage needs. It does not import `acts` —
protocols are an independent domain and must not inherit the act workflow's
role rules by accident.
"""

from accounts.models import UserProfile

from .models import Protocol


def get_user_profile(user):
    if not getattr(user, 'is_authenticated', False):
        return None
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return None
    # An inactive profile grants no application role; superusers are separate.
    if profile.pk is None or not profile.is_active:
        return None
    return profile


def is_protocol_admin(user):
    """The administrator role, with Django's genuine superuser fallback."""
    profile = get_user_profile(user)
    return bool(
        getattr(user, 'is_authenticated', False)
        and (
            getattr(user, 'is_superuser', False)
            or (profile is not None and profile.role == UserProfile.Role.ADMIN)
        )
    )


def can_view_protocol(protocol, user):
    """Every authenticated user may read every protocol."""
    return bool(getattr(user, 'is_authenticated', False))


# The statuses whose content the author may still change. «На доработке» joins
# this tuple when the approval stage lands: the author/administrator rule below
# is already written against the tuple, so nothing else has to move.
EDITABLE_STATUSES = (Protocol.Status.DRAFT,)


def can_edit_protocol(protocol, user):
    """Author or administrator, while the protocol is in an editable status."""
    if protocol.status not in EDITABLE_STATUSES:
        return False
    if is_protocol_admin(user):
        return True
    return bool(getattr(user, 'is_authenticated', False)) and protocol.author_id == user.id


def can_edit_draft_protocol(protocol, user):
    """The draft-only spelling of `can_edit_protocol()`, kept for its callers."""
    if protocol.status != Protocol.Status.DRAFT:
        return False
    return can_edit_protocol(protocol, user)


def can_delete_draft_protocol(protocol, user):
    """Deleting a draft — and so releasing its number — stays author-only.

    Intentionally stricter than editing: the number goes back into the pool,
    so this is not something an administrator does on someone's behalf from
    the user-facing side.
    """
    if protocol.status != Protocol.Status.DRAFT:
        return False
    return bool(getattr(user, 'is_authenticated', False)) and protocol.author_id == user.id
