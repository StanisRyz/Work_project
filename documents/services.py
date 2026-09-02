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

from django.db import DatabaseError, transaction
from django.db.models import Max

from ecosystem.logging_utils import log_event

from .models import (
    CORPORATE_FOLDER_CODE,
    CORPORATE_FOLDER_NAME,
    MAX_FOLDER_DEPTH,
    Document,
    DocumentFolder,
    DocumentHistoryEvent,
    DocumentVersion,
)
from .permissions import (
    can_add_document_version,
    can_create_folder,
    can_delete_document,
    can_delete_folder,
    can_rename_folder,
    can_restore_document_version,
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
        versions = list(DocumentVersion.objects.filter(document__in=documents))
        for version in versions:
            _delete_stored_file(version)
        _record_history(
            [
                _deletion_event(document, user, 'Папка удалена вместе с документом.')
                for document in documents
            ]
        )
        # The FK cascade removes the descendant folders, the document rows and
        # their versions; the history rows survive on a nulled document FK.
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


def _actor(user):
    """The user to record, or None for an anonymous or system caller."""
    return user if getattr(user, 'is_authenticated', False) else None


def _record_history(events):
    """Append history rows. Never raises into the caller's transaction path.

    History is a record of what happened, not a precondition for it: a failure
    to write it must not roll back the upload the user just made. It is logged
    instead, which is where an operator looks when the page disagrees with the
    files.
    """
    if not events:
        return []
    try:
        return DocumentHistoryEvent.objects.bulk_create(events)
    except DatabaseError as exc:
        log_event(
            logger,
            'ERROR',
            'documents.history_failed',
            error_type=type(exc).__name__,
            event_count=len(events),
            outcome='failed',
        )
        return []


def _history_event(document, action, user, description, version=None):
    return DocumentHistoryEvent(
        document=document,
        # Copied, so the row still says what it was about after the document
        # itself is gone.
        document_name=document.name,
        version=version,
        version_number=version.number if version is not None else None,
        action=action,
        user=_actor(user),
        description=description,
    )


def _deletion_event(document, user, description):
    """A deletion event, which must outlive its document.

    `version` is deliberately left unset: the version rows are about to be
    cascaded away, and a FK to one of them would only be nulled a moment later.
    """
    return _history_event(
        document, DocumentHistoryEvent.Action.DOCUMENT_DELETED, user, description
    )


def _version_payload(uploaded_file):
    """The columns copied off an upload, shared by create and add-version."""
    return {
        'original_name': safe_document_name(uploaded_file.name),
        'file_size': uploaded_file.size or 0,
        'content_type': (getattr(uploaded_file, 'content_type', '') or '')[:120],
    }


def upload_document(folder, uploaded_file, user, name='', comment=''):
    """Create a new document in `folder`, with its first version.

    A document and a version are made together and never separately: a
    document with no file is not a state the library has, and every download,
    listing and card assumes there is a current version to describe.

    The upload is validated here as well as in the form — a form is one
    caller, and the rule about what the library may hold belongs to the
    library.
    """
    if not can_upload_document(folder, user):
        raise DocumentError('Недостаточно прав для загрузки документа.')
    if uploaded_file is None:
        raise DocumentError('Выберите файл.')
    validate_document_upload(uploaded_file)

    payload = _version_payload(uploaded_file)
    display_name = safe_document_name(name) if (name or '').strip() else payload['original_name']

    with transaction.atomic():
        document = Document.objects.create(
            folder=folder,
            name=display_name,
            uploaded_by=_actor(user),
        )
        version = DocumentVersion.objects.create(
            document=document,
            file=uploaded_file,
            number=1,
            is_current=True,
            comment=(comment or '').strip(),
            uploaded_by=_actor(user),
            **payload,
        )
        _record_history([
            _history_event(
                document,
                DocumentHistoryEvent.Action.DOCUMENT_CREATED,
                user,
                'Документ создан.',
            ),
            _history_event(
                document,
                DocumentHistoryEvent.Action.VERSION_ADDED,
                user,
                f'Загружена версия {version.label}.',
                version=version,
            ),
        ])

    log_event(
        logger,
        'INFO',
        'documents.uploaded',
        document_id=document.pk,
        folder_id=folder.pk,
        version_number=version.number,
        size_bytes=version.file_size,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return document


def add_document_version(document, uploaded_file, user, comment=''):
    """Add a new current version to an existing document.

    Never an overwrite: a new row is inserted with the next number and its own
    generated storage path, and the previous version only loses `is_current`.
    Every earlier revision stays downloadable exactly as it was uploaded.

    The number is allocated under a row lock on the document, so two
    simultaneous uploads produce v2 and v3 rather than two v2s — and the
    partial unique constraint on `is_current` is the database's own last word
    on «exactly one current version».
    """
    if not can_add_document_version(document, user):
        raise DocumentError('Недостаточно прав для загрузки новой версии.')
    if uploaded_file is None:
        raise DocumentError('Выберите файл.')
    validate_document_upload(uploaded_file)

    payload = _version_payload(uploaded_file)
    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        previous = locked.versions.filter(is_current=True).first()
        next_number = (
            locked.versions.aggregate(highest=Max('number'))['highest'] or 0
        ) + 1
        # Cleared before the insert, so the «one current version» constraint
        # never sees two rows claiming it.
        locked.versions.filter(is_current=True).update(is_current=False)
        version = DocumentVersion.objects.create(
            document=locked,
            file=uploaded_file,
            number=next_number,
            is_current=True,
            comment=(comment or '').strip(),
            uploaded_by=_actor(user),
            **payload,
        )
        # `updated_at` is auto_now, so saving the row is what refreshes it —
        # a new revision is an update to the document even though no column of
        # its own changed.
        locked.save(update_fields=['updated_at'])
        _record_history([
            _history_event(
                locked,
                DocumentHistoryEvent.Action.VERSION_ADDED,
                user,
                f'Загружена версия {version.label}.',
                version=version,
            ),
        ])

    log_event(
        logger,
        'INFO',
        'documents.version_added',
        document_id=document.pk,
        version_number=version.number,
        previous_version=previous.number if previous is not None else None,
        size_bytes=version.file_size,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return version


def restore_document_version(document, version, user):
    """Make an earlier version current again.

    A pointer move, not an edit: no file is copied, rewritten or renumbered,
    and the version that was current stays in the list exactly where it was.
    Restoring the version that is already current is a no-op rather than an
    error — the caller asked for a state, and it already holds.
    """
    if not can_restore_document_version(document, user):
        raise DocumentError('Недостаточно прав для восстановления версии.')
    if version.document_id != document.pk:
        raise DocumentError('Версия принадлежит другому документу.')
    if version.is_current:
        return version

    with transaction.atomic():
        locked = Document.objects.select_for_update().get(pk=document.pk)
        locked.versions.filter(is_current=True).update(is_current=False)
        locked.versions.filter(pk=version.pk).update(is_current=True)
        locked.save(update_fields=['updated_at'])
        _record_history([
            _history_event(
                locked,
                DocumentHistoryEvent.Action.VERSION_RESTORED,
                user,
                f'Версия {version.label} снова стала текущей.',
                version=version,
            ),
        ])

    version.refresh_from_db()
    log_event(
        logger,
        'INFO',
        'documents.version_restored',
        document_id=document.pk,
        version_number=version.number,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return version


def _delete_stored_file(version):
    """Remove one version's blob, tolerating one that is already gone.

    A missing file must not stop the row from being deleted: the point of the
    operation is that neither remains.
    """
    if not version.file:
        return
    try:
        version.file.delete(save=False)
    except OSError as exc:
        log_event(
            logger,
            'WARNING',
            'documents.storage_failed',
            version_id=version.pk,
            document_id=version.document_id,
            operation='delete',
            error_type=type(exc).__name__,
            outcome='failed',
        )


def delete_document(document, user):
    """Delete a document with every one of its versions.

    All or nothing: a controlled document does not survive as a stump of old
    revisions, so the whole chain goes and the history row that records the
    deletion stays behind on a nulled document reference.
    """
    if not can_delete_document(document, user):
        raise DocumentError('Недостаточно прав для удаления документа.')
    folder = document.folder
    document_id = document.pk
    with transaction.atomic():
        versions = list(document.versions.all())
        for version in versions:
            _delete_stored_file(version)
        _record_history([_deletion_event(document, user, 'Документ удалён.')])
        # The cascade takes the version rows; history survives, by SET_NULL.
        document.delete()
    log_event(
        logger,
        'INFO',
        'documents.deleted',
        document_id=document_id,
        folder_id=folder.pk,
        version_count=len(versions),
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return folder
