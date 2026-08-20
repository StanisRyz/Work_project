"""Who may read and change a protocol.

Deliberately small: there is no Protocols UI yet, so this file answers only
the three questions the next stage needs. It does not import `acts` —
protocols are an independent domain and must not inherit the act workflow's
role rules by accident.
"""

from accounts.models import UserProfile

from .models import Protocol, ProtocolApproval


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


# The statuses whose content the author may still change. A protocol returned
# for revision is edited by exactly the same people as a draft — that is the
# whole point of returning it — while `APPROVAL` and `ARCHIVED` are read-only:
# nobody edits a document while it is being signed, or after it is archived.
EDITABLE_STATUSES = (Protocol.Status.DRAFT, Protocol.Status.REVISION)


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


def can_send_protocol_for_approval(protocol, user):
    """Author-only, and only from an editable status.

    Deliberately *not* author-or-Admin. An administrator may fix the content of
    an allowed state, but submitting for approval says the author is finished
    with the document, and nobody makes that statement on their behalf. The
    service re-checks this under the row lock; this is the presentation answer.
    """
    if protocol.status not in EDITABLE_STATUSES:
        return False
    return bool(getattr(user, 'is_authenticated', False)) and protocol.author_id == user.id


def can_decide_protocol_approval(protocol, user):
    """Whether this user has a pending approval on the protocol's current revision.

    One query, used by both «Согласовать» and «Вернуть на доработку»: the two
    actions have exactly the same precondition and differ only in the comment.
    The authoritative check stays in `protocols/services.py`, under the lock.
    """
    if protocol.status != Protocol.Status.APPROVAL:
        return False
    if not getattr(user, 'is_authenticated', False):
        return False
    return ProtocolApproval.objects.filter(
        protocol=protocol,
        revision=protocol.revision,
        user=user,
        status=ProtocolApproval.Status.PENDING,
    ).exists()
