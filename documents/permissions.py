"""Who may read the documentation library and who may change it.

Two levels in this stage, and one helper that decides both:

* an *administrative* user browses folders and downloads files;
* a *document manager* also creates, renames and deletes folders, uploads
  documents and deletes them.

The manager set is a single frozen set of roles, `DOCUMENT_MANAGER_ROLES`,
rather than a superuser check scattered through the views. Granting the future
«Руководство» the same rights over corporate documents is then one line here —
add `UserProfile.Role.MANAGER` to the set — and nothing in `views.py`,
`services.py` or the templates changes. That is the only reason this module
exists as something more than two `if user.is_superuser` lines.

Every rule below is enforced server-side in `documents/views.py`; the template
uses the same helpers only to decide which buttons to draw.
"""

from accounts.models import UserProfile

from .models import CORPORATE_FOLDER_CODE


# The application roles that manage corporate documents. Django's genuine
# superuser is handled separately in `can_manage_documents()` and is not a
# role. To hand the same rights to leadership later, add
# `UserProfile.Role.MANAGER` here — deliberately the whole change.
DOCUMENT_MANAGER_ROLES = frozenset({UserProfile.Role.ADMIN})

# Who may *open* the library at all. Administrative roles only: «Документация»
# is not part of an ordinary employee's working day, and the section is hidden
# from the navigation for everyone outside this set. It is a superset of
# `DOCUMENT_MANAGER_ROLES` by construction — a role that may change documents
# must be able to read them — so it is built from that set rather than
# repeating its members.
DOCUMENT_VIEWER_ROLES = DOCUMENT_MANAGER_ROLES | frozenset({UserProfile.Role.MANAGER})


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
    """The single read rule: an administrative role, or a genuine superuser.

    Every other rule in this module — folders, downloads, versions, history,
    favourites and the «Вложения» branch — is expressed in terms of this one,
    so restricting it here closes the whole library at once, for direct URLs
    as much as for the navigation link that `documents.context_processors`
    hides.
    """
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    profile = get_user_profile(user)
    return profile is not None and profile.role in DOCUMENT_VIEWER_ROLES


def can_manage_documents(user):
    """The single write rule: a managing role, or a genuine superuser."""
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    profile = get_user_profile(user)
    return profile is not None and profile.role in DOCUMENT_MANAGER_ROLES


# ---------------------------------------------------------------------------
# The named rules the views and the template ask for.
#
# All of them currently delegate to the two helpers above. They are spelled out
# separately anyway: each one is a place a later stage can become more specific
# — a folder whose owner may also upload, an attachments branch nobody edits by
# hand — without every call site having to be found again.
# ---------------------------------------------------------------------------


def can_view_folder(folder, user):
    return can_view_documents(user)


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
    """Browsing «Вложения» follows browsing the library: an administrative role.

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
    return can_view_documents(user)


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


def can_delete_document(document, user):
    return can_manage_documents(user)


# ---------------------------------------------------------------------------
# Versions
#
# Corporate documents only. The rules are the two that already existed,
# applied to the new object: reading a version is reading the library, and
# adding or restoring one is managing it. Nothing here widens or narrows what
# a role could already do.
#
# System attachments have no counterpart to any of this. They carry no
# versions, and `can_modify_system_attachments()` refuses every write to them
# for every role — administrators and superusers included.
# ---------------------------------------------------------------------------


def can_view_document_history(document, user):
    """Reading the history follows reading the document."""
    return can_view_documents(user)


def can_download_document_version(version, user):
    """Any earlier revision downloads exactly like the current one.

    Deliberately not manager-only: keeping an old revision readable is the
    point of versioning, and hiding it would make «current» unverifiable.
    """
    return can_view_documents(user)


def can_add_document_version(document, user):
    return can_manage_documents(user)


def can_favorite_document(document, user):
    """Starring a document is a personal bookmark, not a permission.

    Anyone who may read the library may keep their own shortcuts to it — the
    row is private to the user and changes nothing anybody else can see. It is
    stated here anyway so the rule has a name and a place to become stricter.

    System attachments are absent from this by construction: they have no
    `Document` row, so there is nothing to star.
    """
    return can_view_documents(user)


def can_restore_document_version(document, user):
    """Making an earlier revision current again — a management action.

    Restoring never edits or deletes anything: it moves `is_current`, and the
    version that was current stays in the list, downloadable, where it was.
    """
    return can_manage_documents(user)
