"""Who may read the documentation library and who may change it.

Two levels in this stage, and one helper that decides both:

* every authenticated user browses folders and downloads files;
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


# The application roles that manage corporate documents. Django's genuine
# superuser is handled separately in `can_manage_documents()` and is not a
# role. To hand the same rights to leadership later, add
# `UserProfile.Role.MANAGER` here — deliberately the whole change.
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
    """Browsing the library is open to every authenticated user."""
    return bool(getattr(user, 'is_authenticated', False))


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
    """Browsing «Вложения» follows browsing the library: any authenticated user.

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


def can_rename_folder(folder, user):
    """System folders keep their names: the initial structure is not editable.

    A superuser who genuinely needs to rename one does it in Admin, where the
    consequence is visible; the page does not offer it.
    """
    if folder.is_system:
        return False
    return can_manage_documents(user)


def can_delete_folder(folder, user):
    """Same rule as renaming — and deleting takes the whole subtree with it."""
    if folder.is_system:
        return False
    return can_manage_documents(user)


def can_upload_document(folder, user):
    return can_manage_documents(user)


def can_delete_document(document, user):
    return can_manage_documents(user)
