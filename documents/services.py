"""Every write the documentation library performs.

Views parse a request and answer «who is allowed»; this module is the only
place that creates, renames or deletes a folder and that stores or removes a
file. Each function re-asks `documents/permissions.py` under its own name, so
a future caller — a management command, an import script, the attachments
stage — cannot skip the rule by not being a view.

Storage is cleaned up explicitly. Django does not delete a `FileField`'s file
when its row goes away, and a library that accumulates unreferenced blobs is
one nobody can size, so deletions here remove the file first and the row after.
"""

import logging

from django.db import transaction

from ecosystem.logging_utils import log_event

from .models import (
    CORPORATE_FOLDER_CODE,
    CORPORATE_FOLDER_NAME,
    MAX_FOLDER_DEPTH,
    Document,
    DocumentFolder,
)
from .permissions import (
    can_create_folder,
    can_delete_document,
    can_delete_folder,
    can_rename_folder,
    can_upload_document,
)
from .validators import safe_document_name, validate_document_upload


logger = logging.getLogger('ecosystem.documents')


class DocumentError(Exception):
    """A refused library operation, reported to the user as a message."""


# ---------------------------------------------------------------------------
# The initial structure
# ---------------------------------------------------------------------------

# The folders the project ships with, addressed by `code` so the data
# migration is idempotent: re-running it finds the same five rows and can
# never produce a second «Инструкции». They live inside «Корпоративные
# документы», the one writable branch of the library; the other branch,
# «Вложения», is generated from the source attachment tables and is not
# stored here at all.
DEFAULT_FOLDERS = (
    ('instructions', 'Инструкции'),
    ('notes', 'Служебные записки'),
    ('regulatory', 'Нормативные документы'),
    ('training', 'Обучение'),
    ('templates', 'Шаблоны'),
)


def get_corporate_root(folder_model=None):
    """The «Корпоративные документы» folder, or None before it is created."""
    model = folder_model or DocumentFolder
    return model.objects.filter(code=CORPORATE_FOLDER_CODE).first()


def ensure_default_folders(folder_model=None):
    """Create the initial structure if it is missing. Safe to run repeatedly.

    `folder_model` lets a data migration pass its historical model; ordinary
    callers (a deploy check, a test) pass nothing and get the real one.
    Matching on `code` and not on `name` is what makes a second run a no-op
    even after somebody has renamed a folder in Admin.
    """
    model = folder_model or DocumentFolder
    created = []
    corporate, was_created = model.objects.get_or_create(
        code=CORPORATE_FOLDER_CODE,
        defaults={'name': CORPORATE_FOLDER_NAME, 'parent': None, 'is_system': True},
    )
    if was_created:
        created.append(corporate)
    for code, name in DEFAULT_FOLDERS:
        folder, was_created = model.objects.get_or_create(
            code=code,
            defaults={'name': name, 'parent': corporate, 'is_system': True},
        )
        if was_created:
            created.append(folder)
    return created


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


def create_folder(parent, name, user):
    """Create a subfolder of `parent`, or a top-level one when it is None."""
    if not can_create_folder(parent, user):
        raise DocumentError('Недостаточно прав для создания папки.')
    if parent is None:
        # The root holds exactly «Корпоративные документы» and «Вложения»;
        # both are system-defined, and neither is created from the page.
        raise DocumentError(
            'В корне документации новые папки не создаются — откройте '
            '«Корпоративные документы».'
        )
    clean_name = (name or '').strip()
    if not clean_name:
        raise DocumentError('Укажите название папки.')
    if len(clean_name) > 180:
        raise DocumentError('Название папки слишком длинное.')
    if parent is not None and parent.depth + 1 >= MAX_FOLDER_DEPTH:
        raise DocumentError('Достигнута максимальная глубина вложенности папок.')
    if DocumentFolder.objects.filter(parent=parent, name__iexact=clean_name).exists():
        raise DocumentError('Папка с таким названием здесь уже есть.')

    folder = DocumentFolder.objects.create(
        name=clean_name,
        parent=parent,
        created_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    log_event(
        logger,
        'INFO',
        'documents.folder_created',
        folder_id=folder.pk,
        parent_id=parent.pk if parent else None,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return folder


def rename_folder(folder, name, user):
    if not can_rename_folder(folder, user):
        raise DocumentError('Эту папку переименовать нельзя.')
    clean_name = (name or '').strip()
    if not clean_name:
        raise DocumentError('Укажите название папки.')
    if len(clean_name) > 180:
        raise DocumentError('Название папки слишком длинное.')
    if (
        DocumentFolder.objects.filter(parent_id=folder.parent_id, name__iexact=clean_name)
        .exclude(pk=folder.pk)
        .exists()
    ):
        raise DocumentError('Папка с таким названием здесь уже есть.')

    folder.name = clean_name
    folder.save(update_fields=['name', 'updated_at'])
    log_event(
        logger,
        'INFO',
        'documents.folder_renamed',
        folder_id=folder.pk,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return folder


def _subtree_ids(folder):
    """The folder's id and every descendant's, level by level and bounded.

    A plain loop rather than a recursive query: the tree is shallow by
    construction (`MAX_FOLDER_DEPTH`) and this keeps working on every database
    the project supports.
    """
    ids = [folder.pk]
    frontier = [folder.pk]
    for _ in range(MAX_FOLDER_DEPTH):
        if not frontier:
            break
        frontier = list(
            DocumentFolder.objects.filter(parent_id__in=frontier).values_list('pk', flat=True)
        )
        ids.extend(frontier)
    return ids


def delete_folder(folder, user):
    """Delete a folder with everything below it, files included."""
    if not can_delete_folder(folder, user):
        raise DocumentError('Эту папку удалить нельзя.')

    folder_ids = _subtree_ids(folder)
    parent = folder.parent
    with transaction.atomic():
        documents = list(Document.objects.filter(folder_id__in=folder_ids))
        for document in documents:
            _delete_stored_file(document)
        # The FK cascade removes the descendant folders and the document rows.
        folder.delete()
    log_event(
        logger,
        'INFO',
        'documents.folder_deleted',
        folder_id=folder.pk,
        folder_count=len(folder_ids),
        document_count=len(documents),
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return parent


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def upload_document(folder, uploaded_file, user, name=''):
    """Store one uploaded file in `folder`.

    The upload is validated here as well as in the form: a form is one caller,
    and the rule about what the library may hold belongs to the library.
    """
    if not can_upload_document(folder, user):
        raise DocumentError('Недостаточно прав для загрузки документа.')
    if uploaded_file is None:
        raise DocumentError('Выберите файл.')
    validate_document_upload(uploaded_file)

    original_name = safe_document_name(uploaded_file.name)
    display_name = safe_document_name(name) if (name or '').strip() else original_name
    document = Document.objects.create(
        folder=folder,
        file=uploaded_file,
        name=display_name,
        original_name=original_name,
        file_size=uploaded_file.size or 0,
        content_type=(getattr(uploaded_file, 'content_type', '') or '')[:120],
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
    )
    log_event(
        logger,
        'INFO',
        'documents.uploaded',
        document_id=document.pk,
        folder_id=folder.pk,
        size_bytes=document.file_size,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return document


def _delete_stored_file(document):
    """Remove the blob, tolerating one that is already gone.

    A missing file must not stop the row from being deleted: the point of the
    operation is that neither remains.
    """
    if not document.file:
        return
    try:
        document.file.delete(save=False)
    except OSError as exc:
        log_event(
            logger,
            'WARNING',
            'documents.storage_failed',
            document_id=document.pk,
            operation='delete',
            error_type=type(exc).__name__,
            outcome='failed',
        )


def delete_document(document, user):
    if not can_delete_document(document, user):
        raise DocumentError('Недостаточно прав для удаления документа.')
    folder = document.folder
    document_id = document.pk
    with transaction.atomic():
        _delete_stored_file(document)
        document.delete()
    log_event(
        logger,
        'INFO',
        'documents.deleted',
        document_id=document_id,
        folder_id=folder.pk,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return folder
