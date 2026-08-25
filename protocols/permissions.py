"""Who may read and change a protocol.

Deliberately small, and it does not import `acts` — protocols are an
independent domain and must not inherit the act workflow's role rules by
accident. Reading is open to every authenticated user; editing, submitting and
deciding are the author's, the administrator's and the approver's; and
contributing to the collaboration feed stops at the archive.
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


# --------------------------------------------------------------------------
# Collaboration: comments and attachments
#
# Narrow on purpose. The act rules are *not* copied: an act's contribution
# right depends on which department owns the current step, and the protocol
# workflow has no such notion. What a protocol has instead is one line —
# everybody reads it, everybody may contribute to it until it is archived —
# and the archive is immutable, exactly as it is for the document itself.
# --------------------------------------------------------------------------


def can_contribute_to_protocol(protocol, user):
    """Whether this user may add to the protocol's collaboration feed.

    Every authenticated reader may, because everybody may read the protocol and
    a discussion nobody may join is not one. The single exception is `ARCHIVED`:
    an archived protocol is a finished document, and it does not acquire new
    comments or new files afterwards. Deliberately wider than
    `can_edit_protocol()` — commenting is not editing — and deliberately
    unrelated to who has to approve.
    """
    if protocol.status == Protocol.Status.ARCHIVED:
        return False
    return can_view_protocol(protocol, user)


def can_add_protocol_comment(protocol, user):
    return can_contribute_to_protocol(protocol, user)


def can_add_protocol_attachment(protocol, user):
    return can_contribute_to_protocol(protocol, user)


def can_download_protocol_attachment(attachment, user):
    """Reading a file follows reading the protocol, archived ones included.

    An archived protocol stops accepting attachments; it never stops handing
    out the ones it already has.
    """
    return can_view_protocol(attachment.protocol, user)


def can_delete_protocol_attachment(attachment, user):
    """The uploader while the protocol still accepts contributions, or an admin.

    Both go through `can_contribute_to_protocol()` first, so an archived
    protocol's files are read-only for everyone — the administrator included.
    Removing them afterwards is an Admin-site operation, not a page button.
    """
    if not can_contribute_to_protocol(attachment.protocol, user):
        return False
    if is_protocol_admin(user):
        return True
    return bool(
        getattr(user, 'is_authenticated', False)
        and attachment.uploaded_by_id is not None
        and attachment.uploaded_by_id == user.id
    )


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
