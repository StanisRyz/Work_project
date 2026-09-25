"""Who may read the documentation library, which folders, and who may change it.

Three answers, one module:

* **Reading is open.** Every signed-in employee reads the library — the
  plant's instructions, norms and templates are working material, not an
  administrative archive.
* **A folder may be closed** to everyone but some roles
  (`DocumentFolder.allowed_roles`). The restriction inherits downwards — a
  subfolder of a closed folder is closed too — and a document is readable
  exactly when its folder is.
* **Managing** — uploading, editing the card, moving, trashing, requesting
  approval and acknowledgement, closing folders — belongs to the administrator,
  a genuine superuser, and the people flagged «Ответственный за документацию»
  in Django Admin (`UserProfile.is_document_responsible`). A manager sees every
  folder, so a folder can never be closed to the people who keep it.

Every rule below is enforced server-side in `documents/views.py` and
re-asked by `documents/services.py`; the templates use the same helpers only to
decide what to draw. Roles are read through `accounts.roles`, so a role lent by
a substitution opens a closed folder exactly as the profile's own would.
"""

from accounts.models import UserProfile
from accounts.roles import get_user_roles, has_any_role

from .models import CORPORATE_FOLDER_CODE, MAX_FOLDER_DEPTH, DocumentFolder


# The application role that manages documents beside the individually flagged
# people. A genuine superuser is handled separately and is not a role.
DOCUMENT_MANAGER_ROLES = frozenset({UserProfile.Role.ADMIN})


def get_user_profile(user):
    """The user's active profile, or None.

    An inactive profile grants no application role. Mirrors the same helper in
    `acts` and `protocols`; it is repeated rather than imported because the
    documentation library must not inherit either workflow's rules by way of
    an import that later grows.
    """
    if not getattr(user, 'is_authenticated', False):
        return None
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return None
    if profile.pk is None or not profile.is_active:
        return None
    return profile


def can_view_documents(user):
    """The library opens for every signed-in employee.

    What they then see inside it is `can_view_folder()`'s answer, folder by
    folder; this is only «may enter at all», and it is also what draws the
    «Документация» entry in the navigation.
    """
    return bool(getattr(user, 'is_authenticated', False) and getattr(user, 'is_active', True))


def can_manage_documents(user):
    """Administrator, genuine superuser, or «Ответственный за документацию»."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    profile = get_user_profile(user)
    if profile is None:
        return False
    return bool(profile.is_document_responsible) or has_any_role(user, DOCUMENT_MANAGER_ROLES)


# ---------------------------------------------------------------------------
# Folders: open by default, closed to all but some roles when so marked.
# ---------------------------------------------------------------------------


def _folder_open_to(folder, roles):
    """Whether this one folder's own restriction admits someone with `roles`."""
    allowed = folder.allowed_roles or []
    return not allowed or bool(set(allowed) & set(roles))


def can_view_folder(folder, user):
    """The folder and every ancestor admit this user — or they manage documents."""
    if not can_view_documents(user):
        return False
    if can_manage_documents(user):
        return True
    roles = get_user_roles(user)
    chain = [folder, *folder.ancestors()]
    return all(_folder_open_to(entry, roles) for entry in chain)


def visible_folder_ids(user):
    """Every folder id this user may open, in two queries whatever the tree.

    The listing-side twin of `can_view_folder()`: search, «Недавние»,
    favourites and the tree all filter by this set, so a closed folder's
    documents never surface through a side door. The folder table is small by
    nature (a plant's document tree), so reading it whole is cheaper and
    simpler than a recursive query.
    """
    if not can_view_documents(user):
        return frozenset()
    folders = list(DocumentFolder.objects.values('pk', 'parent_id', 'allowed_roles'))
    if can_manage_documents(user):
        return frozenset(row['pk'] for row in folders)
    roles = set(get_user_roles(user))
    by_id = {row['pk']: row for row in folders}
    visible = set()
    for row in folders:
        current, depth, admitted = row, 0, True
        while current is not None and depth <= MAX_FOLDER_DEPTH:
            allowed = current['allowed_roles'] or []
            if allowed and not (set(allowed) & roles):
                admitted = False
                break
            current = by_id.get(current['parent_id'])
            depth += 1
        if admitted:
            visible.add(row['pk'])
    return frozenset(visible)


def can_view_document(document, user):
    """A document is read exactly when its folder is, and never from the trash —
    except by a manager, who is the one who restores it."""
    if document.deleted_at is not None:
        return can_manage_documents(user)
    return can_view_folder(document.folder, user)


def can_set_folder_access(folder, user):
    """Closing or opening a folder is a management action — never on the
    structural root, which must stay readable for the tree to be browsable."""
    return can_manage_documents(user) and not is_structural_folder(folder)


# ---------------------------------------------------------------------------
# The «Вложения» branch
#
# Act, protocol and task attachments are shown here through the read-only
# references in `documents/references.py`. Documentation is a *view* of them:
# the file, its name and its lifetime belong to the act, protocol or task that
# owns it, and the quality history that records it. Changing one from this
# module would edit another domain's record behind its own workflow's back.
#
# So there is no manager exemption and no superuser exemption — the answer is
# False for everybody, permanently, and `can_manage_documents()` is not
# consulted at all. Whoever needs to remove such a file does it where it was
# uploaded, where the owning app writes its history event.
# ---------------------------------------------------------------------------


def can_view_system_attachments(user):
    """Browsing «Вложения» follows browsing the library.

    *Which* attachments are then listed is not decided here — every source
    adapter asks the owning app for the records that user may read, so an act
    invisible in `acts` is invisible here too.
    """
    return can_view_documents(user)


def can_modify_system_attachments(user):
    """Always False. Upload, rename, replace, move and delete, for every role.

    A function and not an inline `False` so the refusal has one name, one
    place, and one docstring saying why — and so a future stage that wants to
    argue with the rule has to change it here, in the open.
    """
    return False


def can_download_document(document, user):
    return can_view_document(document, user)


def can_create_folder(parent, user):
    return can_manage_documents(user)


def is_structural_folder(folder):
    """Whether this folder is part of the library's shape rather than content.

    Exactly one folder is: «Корпоративные документы». It is one of the two
    branches under the browse root — the other, «Вложения», is generated and
    has no row at all — and `create_folder()` refuses to put anything at the
    root, so renaming or removing it would leave the library with nowhere to
    store a document.

    The folders shipped inside it («Инструкции», «Шаблоны», …) are *content*,
    not shape. They are marked `is_system` so the initial structure can be
    recognised and re-created idempotently, and a manager renames and removes
    them like any other folder.
    """
    return folder.code == CORPORATE_FOLDER_CODE


def can_rename_folder(folder, user):
    """Any corporate folder except the structural root."""
    if is_structural_folder(folder):
        return False
    return can_manage_documents(user)


def can_delete_folder(folder, user):
    """Same rule as renaming.

    Whether the folder is actually *empty* is `delete_folder()`'s decision, not
    this one: that is a fact about content and it is re-checked under the
    service's own transaction.
    """
    if is_structural_folder(folder):
        return False
    return can_manage_documents(user)


def can_upload_document(folder, user):
    return can_manage_documents(user)


def can_edit_document(document, user):
    """The card, the name, the folder, the status: a manager's, on a live row."""
    return can_manage_documents(user) and document.deleted_at is None


def can_delete_document(document, user):
    """Sending to «Корзина», restoring from it and purging it."""
    return can_manage_documents(user)


# ---------------------------------------------------------------------------
# Versions
#
# Corporate documents only. Reading a version is reading its document; adding,
# restoring, and sending one for approval or acknowledgement is managing it.
# System attachments have no counterpart to any of this.
# ---------------------------------------------------------------------------


def can_view_document_history(document, user):
    """Reading the history follows reading the document."""
    return can_view_document(document, user)


def can_download_document_version(version, user):
    """Any earlier revision downloads exactly like the current one.

    Deliberately not manager-only: keeping an old revision readable is the
    point of versioning, and hiding it would make «current» unverifiable.
    """
    return can_view_document(version.document, user)


def can_add_document_version(document, user):
    return can_edit_document(document, user)


def can_favorite_document(document, user):
    """Starring is a personal bookmark on something the user may read."""
    return can_view_document(document, user) and document.deleted_at is None


def can_subscribe(user):
    """«Подписаться» — a private request to be told, open to every reader."""
    return can_view_documents(user)


def can_restore_document_version(document, user):
    """Making an earlier revision current again — a management action.

    Restoring never edits or deletes anything: it moves `is_current`, and the
    version that was current stays in the list, downloadable, where it was.
    """
    return can_edit_document(document, user)


def can_request_acknowledgement(document, user):
    return can_edit_document(document, user)


# ---------------------------------------------------------------------------
# Approval, acknowledgement, links
# ---------------------------------------------------------------------------


def can_decide_version(version, user):
    """Whoever holds a `PENDING` approval on this version — nobody else.

    The same shape as `protocols.permissions.can_decide_protocol_approval()`:
    being a manager does not let one sign for somebody else.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if version.approval_status != version.Approval.PENDING or version.document.deleted_at is not None:
        return False
    return version.approvals.filter(user=user, status='PENDING').exists()


def open_acknowledgement_task(document, user):
    """This user's open «Ознакомиться» entry on the document's current version.

    The acknowledgement button exists exactly when this returns a task: a
    person nobody asked has nothing to confirm, and a version already confirmed
    is not confirmed twice.
    """
    from tasks.models import Task

    if not getattr(user, 'is_authenticated', False) or document.deleted_at is not None:
        return None
    return (
        Task.objects.filter(
            source_type=Task.SourceType.DOCUMENT_ACK,
            document_version__document=document,
            document_version__is_current=True,
            individual_assignee=user,
            status__is_final=False,
        )
        .select_related('document_version')
        .first()
    )


def can_link_document_to_act(act, user):
    """Citing a document on an act is contributing to the act."""
    from acts.permissions import can_contribute_to_act

    return can_view_documents(user) and can_contribute_to_act(act, user)


def can_link_document_to_protocol(protocol, user):
    """Citing a document on a protocol is contributing to the protocol."""
    from protocols.permissions import can_contribute_to_protocol

    return can_view_documents(user) and can_contribute_to_protocol(protocol, user)
