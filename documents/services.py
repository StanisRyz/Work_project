"""Every write the documentation library performs.

Views parse a request and answer «who is allowed»; this module is the only
place that creates, renames or deletes a folder and that stores or removes a
file. Each function re-asks `documents/permissions.py` under its own name, so
a future caller — a management command, an import script, the attachments
stage — cannot skip the rule by not being a view.

Storage is cleaned up explicitly. Django does not delete a `FileField`'s file
when its row goes away, and a library that accumulates unreferenced blobs is
one nobody can size, so deletions here remove the row and then — once the
transaction has committed — the file. An upload that fails after its file was
written removes that file again, so a rollback leaves no orphan behind.
"""

import logging
from contextlib import contextmanager
from datetime import timedelta
from functools import partial

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from accounts.models import Department, UserProfile
from accounts.roles import role_holders_q
from accounts.templatetags.people import person_name
from ecosystem.logging_utils import log_event
from ecosystem.workdays import add_working_days
from notifications.services import (
    notify_document_task,
    notify_document_updated,
    notify_document_version_returned,
)
from tasks.models import Task
from tasks.services import (
    cancel_document_tasks,
    complete_document_routing_task,
    create_document_task,
)

from .models import (
    CORPORATE_FOLDER_CODE,
    CORPORATE_FOLDER_NAME,
    MAX_FOLDER_DEPTH,
    TRASH_RETENTION_DAYS,
    Document,
    DocumentFavorite,
    DocumentFolder,
    DocumentHistoryEvent,
    DocumentLink,
    DocumentSubscription,
    DocumentVersion,
    DocumentVersionApproval,
)
from .permissions import (
    can_add_document_version,
    can_create_folder,
    can_delete_document,
    can_delete_folder,
    can_edit_document,
    can_favorite_document,
    can_link_document_to_act,
    can_link_document_to_protocol,
    can_manage_documents,
    can_rename_folder,
    can_request_acknowledgement,
    can_restore_document_version,
    can_set_folder_access,
    can_subscribe,
    can_upload_document,
    can_view_document,
    can_view_folder,
    open_acknowledgement_task,
)
from .text_extraction import extract_version_text
from .validators import safe_document_name, validate_document_upload


def person_label(user):
    return person_name(user) if user is not None else 'Система'


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
    """Delete an **empty** folder.

    Deliberately stricter than the FK cascade allows. This module is an
    archive: removing a folder must never be a way to destroy documents and
    their version history in one click, so a folder that still holds anything —
    a subfolder or a document, at any depth — is refused and the administrator
    is told what is in the way. Emptying it first is an explicit act, document
    by document, each one recorded in the history.
    """
    if not can_delete_folder(folder, user):
        raise DocumentError('Эту папку удалить нельзя.')

    folder_ids = _subtree_ids(folder)
    if Document.objects.filter(folder_id__in=folder_ids).exists():
        raise DocumentError(
            'В папке есть документы — перенесите их или отправьте в корзину, '
            'прежде чем удалять папку.'
        )
    # A trashed document still belongs to its folder — restoring it puts it
    # back there — so a folder is not empty while «Корзина» holds one of its
    # documents.
    if Document.all_objects.filter(folder_id__in=folder_ids).exists():
        raise DocumentError(
            'В корзине есть документы из этой папки — восстановите их или '
            'удалите навсегда, прежде чем удалять папку.'
        )
    if len(folder_ids) > 1:
        raise DocumentError('В папке есть вложенные папки — сначала удалите их.')

    parent = folder.parent
    with transaction.atomic():
        folder.delete()
    log_event(
        logger,
        'INFO',
        'documents.folder_deleted',
        folder_id=folder.pk,
        parent_id=parent.pk if parent is not None else None,
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


@contextmanager
def _discard_files_on_rollback(written):
    """Remove the blobs a failed upload already wrote.

    Storage is not transactional: the file is on disk before the row that
    points at it is committed. If the block fails after that, the rows roll
    back and the file would stay behind in MEDIA_ROOT with nothing referring to
    it. `written` is filled by `_store_version_file()`.
    """
    try:
        yield
    except BaseException:
        for field in written:
            try:
                field.delete(save=False)
            except OSError:
                log_event(
                    logger,
                    'WARNING',
                    'documents.storage_failed',
                    operation='rollback_cleanup',
                    outcome='failed',
                )
        raise


def _store_version_file(version, uploaded_file, written):
    """Write the blob, remember it for rollback cleanup, read its text, then
    insert the row."""
    version.file.save(uploaded_file.name, uploaded_file, save=False)
    written.append(version.file)
    # Extracted once, here, so «поиск по тексту» never opens a file at query
    # time. A file nothing can be read from simply gets no text.
    version.text_content = extract_version_text(version)
    version.save()
    return version


def _version_payload(uploaded_file):
    """The columns copied off an upload, shared by create and add-version."""
    return {
        'original_name': safe_document_name(uploaded_file.name),
        'file_size': uploaded_file.size or 0,
        'content_type': (getattr(uploaded_file, 'content_type', '') or '')[:120],
    }


def _clean_roles(roles):
    valid = {value for value, _label in UserProfile.Role.choices}
    return sorted({str(role) for role in (roles or []) if str(role) in valid})


def _clean_department_ids(department_ids):
    ids = set()
    for value in department_ids or []:
        try:
            ids.add(int(value))
        except (TypeError, ValueError):
            continue
    return sorted(Department.objects.filter(pk__in=ids, is_active=True).values_list('pk', flat=True))


def upload_document(
    folder, uploaded_file, user, name='', comment='', *, ack_roles=(), ack_department_ids=(),
):
    """Create a new document in `folder`, with its first version.

    A document and a version are made together and never separately: a
    document with no file is not a state the library has, and every download,
    listing and card assumes there is a current version to describe. The first
    version is therefore current at once — an approval round belongs to a
    *new* revision of a document already in force.

    `ack_roles`/`ack_department_ids` are the uploader's «ознакомить»: the
    acknowledgement tasks are issued in the same transaction.
    """
    if not can_upload_document(folder, user):
        raise DocumentError('Недостаточно прав для загрузки документа.')
    if uploaded_file is None:
        raise DocumentError('Выберите файл.')
    validate_document_upload(uploaded_file)

    payload = _version_payload(uploaded_file)
    display_name = safe_document_name(name) if (name or '').strip() else payload['original_name']

    written = []
    with _discard_files_on_rollback(written), transaction.atomic():
        document = Document.objects.create(
            folder=folder,
            name=display_name,
            uploaded_by=_actor(user),
        )
        version = DocumentVersion(
            document=document,
            number=1,
            is_current=True,
            comment=(comment or '').strip(),
            uploaded_by=_actor(user),
            ack_roles=_clean_roles(ack_roles),
            ack_department_ids=_clean_department_ids(ack_department_ids),
            **payload,
        )
        _store_version_file(version, uploaded_file, written)
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
        _put_version_in_force(document, version, user, previous=None)

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


def upload_documents(folder, uploaded_files, user, comment='', *, ack_roles=(), ack_department_ids=()):
    """Store a selection of files as that many separate documents.

    One transaction each rather than one for the whole batch: a selection of
    ten files is ten independent documents, and the ninth being refused is no
    reason to discard the eight that were fine. Returns the documents created
    and the per-file refusals, so the view can report both.
    """
    if not can_upload_document(folder, user):
        raise DocumentError('Недостаточно прав для загрузки документа.')
    files = [item for item in (uploaded_files or []) if item is not None]
    if not files:
        raise DocumentError('Выберите файл.')

    created, errors = [], []
    for uploaded_file in files:
        try:
            created.append(upload_document(
                folder, uploaded_file, user, comment=comment,
                ack_roles=ack_roles, ack_department_ids=ack_department_ids,
            ))
        except (DocumentError, ValidationError) as exc:
            errors.append(f'{safe_document_name(uploaded_file.name)}: {_error_text(exc)}')
    return created, errors


def _error_text(exc):
    """The message of a `DocumentError` or a field `ValidationError`."""
    if isinstance(exc, ValidationError):
        return '; '.join(exc.messages)
    return str(exc)


def _lock_document(document):
    """Re-read and row-lock the document, trashed or not."""
    return Document.all_objects.select_for_update().get(pk=document.pk)


def add_document_version(
    document, uploaded_file, user, comment='', *, revision_label='', approver_ids=(),
    ack_roles=(), ack_department_ids=(),
):
    """Add a new version to an existing document.

    Never an overwrite: a new row is inserted with the next number and its own
    generated storage path. Every earlier revision stays downloadable exactly as
    it was uploaded.

    «Что изменилось» is required: a controlled document's revision without a
    stated change is a revision nobody can review.

    Without approvers the version is current at once, as it always was. With
    them it is stored `PENDING` and **not** current: the document keeps reading
    as its previous version until the last approver agrees
    (`approve_version()`), and a document has at most one version on approval
    at a time. The uploader never approves their own version.

    The number is allocated under a row lock on the document, so two
    simultaneous uploads produce v2 and v3 rather than two v2s — and the
    partial unique constraint on `is_current` is the database's own last word
    on «exactly one current version».
    """
    if not can_add_document_version(document, user):
        raise DocumentError('Недостаточно прав для загрузки новой версии.')
    if uploaded_file is None:
        raise DocumentError('Выберите файл.')
    clean_comment = (comment or '').strip()
    if not clean_comment:
        raise DocumentError('Опишите, что изменилось в новой версии.')
    validate_document_upload(uploaded_file)

    approvers = list(
        User.objects.filter(pk__in=[int(pk) for pk in approver_ids if str(pk).isdigit()], is_active=True)
        .exclude(pk=getattr(user, 'pk', None))
        .order_by('pk')
    )
    payload = _version_payload(uploaded_file)
    written = []
    with _discard_files_on_rollback(written), transaction.atomic():
        locked = _lock_document(document)
        if locked.deleted_at is not None:
            raise DocumentError('Документ в корзине — сначала восстановите его.')
        if locked.versions.filter(approval_status=DocumentVersion.Approval.PENDING).exists():
            raise DocumentError(
                'У документа есть версия на согласовании — дождитесь решения, '
                'прежде чем загружать следующую.'
            )
        previous = locked.versions.filter(is_current=True).first()
        next_number = (
            locked.versions.aggregate(highest=Max('number'))['highest'] or 0
        ) + 1
        version = DocumentVersion(
            document=locked,
            number=next_number,
            is_current=False,
            comment=clean_comment,
            revision_label=(revision_label or '').strip()[:40],
            uploaded_by=_actor(user),
            approval_status=DocumentVersion.Approval.PENDING if approvers else DocumentVersion.Approval.NONE,
            ack_roles=_clean_roles(ack_roles),
            ack_department_ids=_clean_department_ids(ack_department_ids),
            **payload,
        )
        _store_version_file(version, uploaded_file, written)
        events = [
            _history_event(
                locked,
                DocumentHistoryEvent.Action.VERSION_ADDED,
                user,
                f'Загружена версия {version.full_label}: {clean_comment}',
                version=version,
            ),
        ]
        if approvers:
            _open_approval_round(locked, version, approvers, user)
            events.append(_history_event(
                locked,
                DocumentHistoryEvent.Action.VERSION_SUBMITTED,
                user,
                'Версия отправлена на согласование: '
                + ', '.join(person_label(person) for person in approvers) + '.',
                version=version,
            ))
            locked.save(update_fields=['updated_at'])
        else:
            _make_current(locked, version)
        _record_history(events)
        if not approvers:
            _put_version_in_force(locked, version, user, previous=previous)

    log_event(
        logger,
        'INFO',
        'documents.version_added',
        document_id=document.pk,
        version_number=version.number,
        previous_version=previous.number if previous is not None else None,
        approver_count=len(approvers),
        size_bytes=version.file_size,
        user_id=getattr(user, 'pk', None),
        outcome='ok',
    )
    return version


def _make_current(locked, version):
    """Move `is_current` to `version`. Cleared first, so the partial unique
    constraint never sees two rows claiming it."""
    locked.versions.filter(is_current=True).exclude(pk=version.pk).update(is_current=False)
    DocumentVersion.objects.filter(pk=version.pk).update(is_current=True)
    version.is_current = True
    # `updated_at` is auto_now: saving the row is what refreshes it — a new
    # revision is an update to the document even though no column of its own
    # changed.
    locked.save(update_fields=['updated_at'])


def _put_version_in_force(document, version, user, *, previous):
    """Everything that follows a version becoming the one people read.

    Acknowledgements of the version it replaced are withdrawn — confirming an
    outdated text is worthless — the uploader's «ознакомить» is issued, and the
    subscribers are told. Called inside the caller's transaction, after the
    version is current.
    """
    if previous is not None and previous.pk != version.pk:
        cancel_document_tasks(
            Task.objects.select_for_update().filter(
                source_type=Task.SourceType.DOCUMENT_ACK, document_version=previous,
            ).select_related('status').order_by('pk'),
            actor=_actor(user),
            reason=f'Вышла новая версия документа ({version.full_label}).',
        )
    if version.ack_roles or version.ack_department_ids:
        _issue_acknowledgements(document, version, user, version.ack_roles, version.ack_department_ids)
    # A new document tells the subscribers of its folder, a new version
    # those of the document too.
    subscribers = [person for person in subscribers_of(document) if person.pk != getattr(user, 'pk', None)]
    if subscribers:
        notify_document_updated(version, _actor(user), subscribers)


# ---------------------------------------------------------------------------
# Approval — a new version agreed before it comes into force
#
# The protocol approval's shape at the size of one file: a row and a queue
# entry per approver, the last approval makes the version current in the same
# transaction, a return (with a reason) closes the round and leaves the version
# a readable, non-current row. Lock order is document → approvals → tasks.
# ---------------------------------------------------------------------------


APPROVAL_WORKING_DAYS = 2
ACKNOWLEDGEMENT_WORKING_DAYS = 5


def _open_approval_round(document, version, approvers, user):
    due_date = add_working_days(timezone.localdate(), APPROVAL_WORKING_DAYS)
    for approver in approvers:
        task = create_document_task(
            Task.SourceType.DOCUMENT_APPROVAL,
            version,
            approver,
            created_by=user,
            due_date=due_date,
            task_text=f'Согласовать версию {version.full_label} документа «{document.title}».',
            department=document.owner_department,
        )
        DocumentVersionApproval.objects.create(version=version, user=approver, task=task)
        notify_document_task(task, _actor(user))


def _lock_pending_version(version):
    locked_document = _lock_document(version.document)
    locked_version = DocumentVersion.objects.get(pk=version.pk)
    if locked_document.deleted_at is not None:
        raise DocumentError('Документ в корзине.')
    if locked_version.approval_status != DocumentVersion.Approval.PENDING:
        raise DocumentError('Эта версия уже не на согласовании.')
    return locked_document, locked_version


def _pending_approval(version, user):
    approval = (
        DocumentVersionApproval.objects.select_for_update()
        .filter(version=version, user=user, status=DocumentVersionApproval.Status.PENDING)
        .first()
    )
    if approval is None:
        raise DocumentError('Вы не участвуете в согласовании этой версии или уже приняли решение.')
    return approval


def approve_version(version, user):
    """Agree to a version. The last agreement puts it in force."""
    now = timezone.now()
    with transaction.atomic():
        document, version = _lock_pending_version(version)
        approval = _pending_approval(version, user)
        approval.status = DocumentVersionApproval.Status.APPROVED
        approval.decided_at = now
        approval.save(update_fields=['status', 'decided_at'])
        complete_document_routing_task(_locked_task(approval.task_id), user=user, closed_at=now)
        events = [_history_event(
            document, DocumentHistoryEvent.Action.VERSION_APPROVED, user,
            f'{person_label(user)} согласовал(а) версию {version.full_label}.', version=version,
        )]
        finished = not version.approvals.filter(status=DocumentVersionApproval.Status.PENDING).exists()
        if finished:
            previous = document.versions.filter(is_current=True).first()
            version.approval_status = DocumentVersion.Approval.APPROVED
            version.save(update_fields=['approval_status'])
            _make_current(document, version)
            events.append(_history_event(
                document, DocumentHistoryEvent.Action.VERSION_APPROVED, user,
                f'Версия {version.full_label} согласована всеми и стала текущей.', version=version,
            ))
            _record_history(events)
            _put_version_in_force(document, version, user, previous=previous)
        else:
            _record_history(events)
    log_event(
        logger, 'INFO', 'documents.version_approved',
        document_id=document.pk, version_number=version.number,
        user_id=getattr(user, 'pk', None), finished=finished, outcome='ok',
    )
    return finished


def return_version(version, user, comment):
    """Send a version back with a reason. The rest of the round is withdrawn."""
    reason = (comment or '').strip()
    if not reason:
        raise DocumentError('Укажите причину возврата.')
    now = timezone.now()
    with transaction.atomic():
        document, version = _lock_pending_version(version)
        approval = _pending_approval(version, user)
        approval.status = DocumentVersionApproval.Status.RETURNED
        approval.comment = reason
        approval.decided_at = now
        approval.save(update_fields=['status', 'comment', 'decided_at'])
        complete_document_routing_task(_locked_task(approval.task_id), user=user, closed_at=now)
        _withdraw_approval_round(version, user, reason='Версия возвращена другим согласующим.')
        version.approval_status = DocumentVersion.Approval.RETURNED
        version.approval_comment = reason
        version.save(update_fields=['approval_status', 'approval_comment'])
        document.save(update_fields=['updated_at'])
        _record_history([_history_event(
            document, DocumentHistoryEvent.Action.VERSION_RETURNED, user,
            f'{person_label(user)} вернул(а) версию {version.full_label}: {reason}', version=version,
        )])
        notify_document_version_returned(version, _actor(user))
    log_event(
        logger, 'INFO', 'documents.version_returned',
        document_id=document.pk, version_number=version.number,
        user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return version


def _locked_task(task_id):
    if task_id is None:
        return None
    return Task.objects.select_for_update().select_related('status').filter(pk=task_id).first()


def _withdraw_approval_round(version, user, *, reason):
    """Cancel every still-pending approval of `version` and its queue entry."""
    pending = list(
        DocumentVersionApproval.objects.select_for_update()
        .filter(version=version, status=DocumentVersionApproval.Status.PENDING)
        .order_by('pk')
    )
    now = timezone.now()
    for row in pending:
        row.status = DocumentVersionApproval.Status.CANCELLED
        row.decided_at = now
        row.save(update_fields=['status', 'decided_at'])
    task_ids = [row.task_id for row in pending if row.task_id]
    if task_ids:
        cancel_document_tasks(
            Task.objects.select_for_update().filter(pk__in=task_ids).select_related('status').order_by('pk'),
            actor=_actor(user), reason=reason,
        )


# ---------------------------------------------------------------------------
# Acknowledgement — «Ознакомиться» / «Ознакомлен»
# ---------------------------------------------------------------------------


def acknowledgement_recipients(roles, department_ids):
    """Active employees holding any of `roles` or working in any of the
    departments — the same «today» the role helpers answer."""
    condition = Q(pk__in=[])
    for role in roles or ():
        condition |= role_holders_q(role)
    if department_ids:
        condition |= Q(userprofile__department_id__in=department_ids)
    return (
        User.objects.filter(condition, is_active=True, userprofile__is_active=True)
        .distinct()
        .order_by('last_name', 'first_name', 'username')
    )


def _issue_acknowledgements(document, version, user, roles, department_ids):
    """One personal «Ознакомиться» task per recipient not already asked.

    The requester is left out — they uploaded the text — and a person already
    holding a task for this version (open or done) is skipped, which with
    `unique_document_ack_task` makes a repeated request harmless.
    """
    asked = set(
        Task.objects.filter(source_type=Task.SourceType.DOCUMENT_ACK, document_version=version)
        .values_list('individual_assignee_id', flat=True)
    )
    due_date = add_working_days(timezone.localdate(), ACKNOWLEDGEMENT_WORKING_DAYS)
    created = []
    for person in acknowledgement_recipients(roles, department_ids):
        if person.pk in asked or person.pk == getattr(user, 'pk', None):
            continue
        # A closed folder is closed for acknowledgement too: nobody is asked
        # to read what they may not open.
        if not can_view_document(document, person):
            continue
        task = create_document_task(
            Task.SourceType.DOCUMENT_ACK,
            version,
            person,
            created_by=user,
            due_date=due_date,
            task_text=f'Ознакомиться с документом «{document.title}» ({version.full_label}).',
            department=document.owner_department,
        )
        notify_document_task(task, _actor(user))
        created.append(task)
    return created


def request_acknowledgement(document, user, *, roles=(), department_ids=()):
    """Ask more people to read the current version. Returns how many were asked."""
    if not can_request_acknowledgement(document, user):
        raise DocumentError('Недостаточно прав для рассылки на ознакомление.')
    clean_roles = _clean_roles(roles)
    clean_departments = _clean_department_ids(department_ids)
    if not clean_roles and not clean_departments:
        raise DocumentError('Выберите роли или подразделения, которых нужно ознакомить.')
    with transaction.atomic():
        locked = _lock_document(document)
        version = locked.versions.filter(is_current=True).first()
        if version is None or locked.deleted_at is not None:
            raise DocumentError('У документа нет действующей версии.')
        version.ack_roles = sorted(set(version.ack_roles or []) | set(clean_roles))
        version.ack_department_ids = sorted(set(version.ack_department_ids or []) | set(clean_departments))
        version.save(update_fields=['ack_roles', 'ack_department_ids'])
        created = _issue_acknowledgements(locked, version, user, clean_roles, clean_departments)
        if created:
            _record_history([_history_event(
                locked, DocumentHistoryEvent.Action.ACK_REQUESTED, user,
                f'Разослан на ознакомление: {len(created)} чел.', version=version,
            )])
    log_event(
        logger, 'INFO', 'documents.ack_requested',
        document_id=document.pk, recipient_count=len(created),
        user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return len(created)


def acknowledge_document(document, user):
    """«Ознакомлен»: close this user's open acknowledgement of the current version."""
    with transaction.atomic():
        _lock_document(document)
        task = open_acknowledgement_task(document, user)
        if task is None:
            raise DocumentError('Запроса на ознакомление с этой версией для вас нет.')
        complete_document_routing_task(_locked_task(task.pk), user=user, closed_at=timezone.now())
    log_event(
        logger, 'INFO', 'documents.acknowledged',
        document_id=document.pk, task_id=task.pk, user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return task


# ---------------------------------------------------------------------------
# Review reminders
# ---------------------------------------------------------------------------


REVIEW_LEAD_DAYS = 30


def create_review_tasks(today=None, *, lead_days=REVIEW_LEAD_DAYS):
    """Raise «Пересмотреть документ» for every review date within `lead_days`.

    Run daily by `manage.py document_review_reminders`. A document in force,
    with a responsible person and a current version, whose review date is due
    within the window, gets one task on that version with the review date as
    its deadline — `unique_document_review_task` and the existence check make
    a second run a no-op. Moving the review date is what asks for the next one.
    """
    today = today or timezone.localdate()
    horizon = today + timedelta(days=lead_days)
    documents = (
        Document.objects.filter(
            status=Document.Status.ACTIVE,
            review_date__isnull=False,
            review_date__lte=horizon,
            responsible__isnull=False,
            responsible__is_active=True,
        )
        .select_related('responsible', 'owner_department')
        .order_by('pk')
    )
    created = []
    for document in documents:
        version = document.current_version
        if version is None:
            continue
        if Task.objects.filter(
            source_type=Task.SourceType.DOCUMENT_REVIEW,
            document_version__document=document,
            due_date=document.review_date,
        ).exists():
            continue
        with transaction.atomic():
            task = create_document_task(
                Task.SourceType.DOCUMENT_REVIEW,
                version,
                document.responsible,
                created_by=document.responsible,
                due_date=document.review_date,
                task_text=(
                    f'Пересмотреть документ «{document.title}» до '
                    f'{document.review_date:%d.%m.%Y}: подтвердить актуальность, '
                    'загрузить новую версию или перенести дату пересмотра.'
                ),
                department=document.owner_department,
            )
            notify_document_task(task, None)
        created.append(task)
    log_event(logger, 'INFO', 'documents.review_tasks', created_count=len(created), outcome='ok')
    return created


# ---------------------------------------------------------------------------
# The card, the folder, the status
# ---------------------------------------------------------------------------


CARD_FIELDS = (
    ('name', 'Название'),
    ('designation', 'Обозначение'),
    ('effective_date', 'Дата введения'),
    ('review_date', 'Дата пересмотра'),
    ('owner_department', 'Подразделение-владелец'),
    ('responsible', 'Ответственный'),
)


def update_document_card(document, user, *, status=None, cancellation_reason='', **values):
    """Change the card. One `CARD_UPDATED` event naming the changed fields, and
    a separate `STATUS_CHANGED` when the status moved.

    Cancelling needs a reason and keeps the document — withdrawn, readable, and
    still cited by whatever referred to it; its open acknowledgement, approval
    and review tasks are withdrawn with it. Returning a cancelled document to
    force clears the cancellation.
    """
    if not can_edit_document(document, user):
        raise DocumentError('Недостаточно прав для изменения карточки документа.')
    with transaction.atomic():
        locked = _lock_document(document)
        changed = []
        for field, label in CARD_FIELDS:
            if field not in values:
                continue
            value = values[field]
            if field == 'name':
                value = safe_document_name(value) if (value or '').strip() else ''
                if not value:
                    raise DocumentError('Укажите название документа.')
            if field == 'designation':
                value = (value or '').strip()[:80]
            if getattr(locked, field) != value:
                setattr(locked, field, value)
                changed.append(label)
        events = []
        if status is not None and status != locked.status:
            if status not in Document.Status.values:
                raise DocumentError('Неизвестный статус документа.')
            reason = (cancellation_reason or '').strip()
            if status == Document.Status.CANCELLED:
                if not reason:
                    raise DocumentError('Укажите причину отмены документа.')
                locked.cancelled_at = timezone.now()
                locked.cancelled_by = _actor(user)
                locked.cancellation_reason = reason
                _withdraw_open_work(locked, user, reason='Документ отменён.')
            else:
                locked.cancelled_at = None
                locked.cancelled_by = None
                locked.cancellation_reason = ''
            previous_label = locked.get_status_display()
            locked.status = status
            description = f'Статус: {previous_label} → {locked.get_status_display()}.'
            if status == Document.Status.CANCELLED:
                description += f' Причина: {reason}'
            events.append(_history_event(locked, DocumentHistoryEvent.Action.STATUS_CHANGED, user, description))
        if changed:
            events.insert(0, _history_event(
                locked, DocumentHistoryEvent.Action.CARD_UPDATED, user, 'Изменено: ' + ', '.join(changed) + '.',
            ))
        if events:
            locked.save()
            _record_history(events)
    return locked


def _withdraw_open_work(document, user, *, reason):
    """Cancel every open task of the document and any approval round in flight."""
    for version in document.versions.filter(approval_status=DocumentVersion.Approval.PENDING):
        _withdraw_approval_round(version, user, reason=reason)
        version.approval_status = DocumentVersion.Approval.RETURNED
        version.approval_comment = reason
        version.save(update_fields=['approval_status', 'approval_comment'])
    cancel_document_tasks(
        Task.objects.select_for_update()
        .filter(document_version__document=document, status__is_final=False)
        .select_related('status').order_by('pk'),
        actor=_actor(user), reason=reason,
    )


def move_documents(documents, target_folder, user):
    """Move documents into another folder. The files stay where they are on
    disk — the stored path is an opaque key, not the folder."""
    if not can_upload_document(target_folder, user):
        raise DocumentError('Недостаточно прав для переноса документов.')
    moved = 0
    with transaction.atomic():
        for document in documents:
            if not can_edit_document(document, user):
                raise DocumentError('Недостаточно прав для переноса документа.')
            locked = _lock_document(document)
            if locked.folder_id == target_folder.pk:
                continue
            source_name = locked.folder.name
            locked.folder = target_folder
            locked.save(update_fields=['folder', 'updated_at'])
            _record_history([_history_event(
                locked, DocumentHistoryEvent.Action.MOVED, user,
                f'Перенесён из «{source_name}» в «{target_folder.name}».',
            )])
            moved += 1
    log_event(
        logger, 'INFO', 'documents.moved',
        folder_id=target_folder.pk, document_count=moved, user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return moved


def set_folder_access(folder, roles, user):
    """Close a folder to all but `roles`, or open it again with none."""
    if not can_set_folder_access(folder, user):
        raise DocumentError('Доступ к этой папке изменить нельзя.')
    folder.allowed_roles = _clean_roles(roles)
    folder.save(update_fields=['allowed_roles', 'updated_at'])
    log_event(
        logger, 'INFO', 'documents.folder_access_set',
        folder_id=folder.pk, role_count=len(folder.allowed_roles),
        user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return folder


def restore_document_version(document, version, user):
    """Make an earlier version current again.

    A pointer move, not an edit: no file is copied, rewritten or renumbered,
    and the version that was current stays in the list exactly where it was.
    Restoring the version that is already current is a no-op rather than an
    error — the caller asked for a state, and it already holds. A version on
    approval or returned from it was never in force and cannot be «restored».
    """
    if not can_restore_document_version(document, user):
        raise DocumentError('Недостаточно прав для восстановления версии.')
    if version.document_id != document.pk:
        raise DocumentError('Версия принадлежит другому документу.')
    if version.is_current:
        return version
    if version.approval_status in (DocumentVersion.Approval.PENDING, DocumentVersion.Approval.RETURNED):
        raise DocumentError('Эта версия не проходила согласование и не может стать текущей.')

    with transaction.atomic():
        locked = _lock_document(document)
        _make_current(locked, version)
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


# ---------------------------------------------------------------------------
# «Корзина»
#
# Deleting a document sends it here; nothing is lost for
# `TRASH_RETENTION_DAYS`. Restoring puts it back in its folder as it was. The
# final removal — by hand from the trash page, or by `purge_document_trash`
# once the period is over — deletes the rows and then the files, and refuses a
# document that tasks were issued on: those tasks are records, and `PROTECT`
# on `Task.document_version` keeps the version they name.
# ---------------------------------------------------------------------------


def trash_documents(documents, user):
    trashed = 0
    with transaction.atomic():
        for document in documents:
            if not can_delete_document(document, user):
                raise DocumentError('Недостаточно прав для удаления документа.')
            locked = _lock_document(document)
            if locked.deleted_at is not None:
                continue
            _withdraw_open_work(locked, user, reason='Документ отправлен в корзину.')
            locked.deleted_at = timezone.now()
            locked.deleted_by = _actor(user)
            locked.save(update_fields=['deleted_at', 'deleted_by', 'updated_at'])
            _record_history([_history_event(
                locked, DocumentHistoryEvent.Action.TRASHED, user, 'Документ отправлен в корзину.',
            )])
            trashed += 1
    log_event(
        logger, 'INFO', 'documents.trashed',
        document_count=trashed, user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return trashed


def trash_document(document, user):
    trash_documents([document], user)
    return document.folder


def restore_document(document, user):
    if not can_delete_document(document, user):
        raise DocumentError('Недостаточно прав для восстановления документа.')
    with transaction.atomic():
        locked = _lock_document(document)
        if locked.deleted_at is None:
            return locked
        locked.deleted_at = None
        locked.deleted_by = None
        locked.save(update_fields=['deleted_at', 'deleted_by', 'updated_at'])
        _record_history([_history_event(
            locked, DocumentHistoryEvent.Action.RESTORED, user, 'Документ восстановлен из корзины.',
        )])
    log_event(
        logger, 'INFO', 'documents.restored',
        document_id=document.pk, user_id=getattr(user, 'pk', None), outcome='ok',
    )
    return locked


def can_purge(document):
    """A document tasks were issued on is a record and is never purged."""
    return not Task.objects.filter(document_version__document=document).exists()


def purge_document(document, user):
    """Delete a trashed document with every one of its versions, for good.

    All or nothing: a controlled document does not survive as a stump of old
    revisions, so the whole chain goes and the history row that records the
    deletion stays behind on a nulled document reference.
    """
    if user is not None and not can_delete_document(document, user):
        raise DocumentError('Недостаточно прав для удаления документа.')
    if document.deleted_at is None:
        raise DocumentError('Навсегда удаляется только документ из корзины.')
    if not can_purge(document):
        raise DocumentError(
            'По документу выдавались задачи (ознакомление, согласование или пересмотр) — '
            'он остаётся в корзине как запись и навсегда не удаляется.'
        )
    folder = document.folder
    document_id = document.pk
    with transaction.atomic():
        versions = list(document.versions.all())
        _record_history([_deletion_event(document, user, 'Документ удалён навсегда.')])
        # The cascade takes the version rows; history survives, by SET_NULL.
        document.delete()
        # The blobs go only once the rows are really gone. Deleted inside the
        # block, a rollback would have left every version row pointing at a
        # file that no longer exists.
        for version in versions:
            transaction.on_commit(partial(_delete_stored_file, version))
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


def purge_expired_trash(now=None):
    """What `manage.py purge_document_trash` does: remove what has waited long
    enough, skip what is a record. Returns `(purged, kept)`."""
    now = now or timezone.now()
    cutoff = now - timedelta(days=TRASH_RETENTION_DAYS)
    purged = kept = 0
    for document in Document.all_objects.filter(deleted_at__lte=cutoff).select_related('folder').order_by('pk'):
        if not can_purge(document):
            kept += 1
            continue
        purge_document(document, None)
        purged += 1
    return purged, kept


# ---------------------------------------------------------------------------
# «Где используется» and subscriptions
# ---------------------------------------------------------------------------


def link_document(document, user, *, act=None, protocol=None):
    """Cite a document on an act or a protocol. Idempotent."""
    if (act is None) == (protocol is None):
        raise DocumentError('Документ связывается ровно с одним актом или протоколом.')
    if not can_view_document(document, user) or document.deleted_at is not None:
        raise DocumentError('Документ недоступен.')
    if act is not None and not can_link_document_to_act(act, user):
        raise DocumentError('Недостаточно прав, чтобы добавить документ к акту.')
    if protocol is not None and not can_link_document_to_protocol(protocol, user):
        raise DocumentError('Недостаточно прав, чтобы добавить документ к протоколу.')
    with transaction.atomic():
        link, created = DocumentLink.objects.get_or_create(
            document=document, act=act, protocol=protocol, defaults={'created_by': _actor(user)},
        )
        if created:
            target = f'акт {act.number}' if act is not None else f'протокол {protocol}'
            _record_history([_history_event(
                document, DocumentHistoryEvent.Action.LINKED, user, f'Указан как основание: {target}.',
            )])
    return link


def unlink_document(link, user):
    allowed = (
        can_link_document_to_act(link.act, user) if link.act_id
        else can_link_document_to_protocol(link.protocol, user)
    )
    if not allowed and not can_manage_documents(user):
        raise DocumentError('Недостаточно прав, чтобы убрать связь.')
    with transaction.atomic():
        target = f'акт {link.act.number}' if link.act_id else f'протокол {link.protocol}'
        _record_history([_history_event(
            link.document, DocumentHistoryEvent.Action.UNLINKED, user, f'Связь убрана: {target}.',
        )])
        link.delete()


def toggle_subscription(user, *, document=None, folder=None):
    """Subscribe to or unsubscribe from a document or a folder. Returns the new state."""
    if (document is None) == (folder is None):
        raise DocumentError('Подписка оформляется на документ или на папку.')
    if not can_subscribe(user):
        raise DocumentError('Недостаточно прав для подписки.')
    if document is not None and not can_view_document(document, user):
        raise DocumentError('Документ недоступен.')
    if folder is not None and not can_view_folder(folder, user):
        raise DocumentError('Папка недоступна.')
    existing = DocumentSubscription.objects.filter(user=user, document=document, folder=folder).first()
    if existing is not None:
        existing.delete()
        return False
    DocumentSubscription.objects.get_or_create(user=user, document=document, folder=folder)
    return True


def subscribers_of(document):
    """Everyone subscribed to the document or to any folder above it who may
    still read it."""
    folder_ids = [folder.pk for folder in document.folder.breadcrumbs()]
    users = (
        User.objects.filter(
            Q(document_subscriptions__document=document)
            | Q(document_subscriptions__folder_id__in=folder_ids),
            is_active=True,
        )
        .distinct()
        .order_by('pk')
    )
    return [person for person in users if can_view_document(document, person)]


# ---------------------------------------------------------------------------
# Favourites
#
# A personal shortcut, not a permission and not shared state: a row here says
# «this user keeps a link to this document» and nothing else. Corporate
# documents only — a system attachment has no `Document` row to point at.
# ---------------------------------------------------------------------------


def toggle_document_favorite(document, user):
    """Star or unstar one document for one user. Returns the new state.

    Idempotent in both directions: starring what is already starred and
    unstarring what is not are both no-ops that report the state truthfully,
    so a double-submitted form cannot produce a duplicate row or an error.
    """
    if not can_favorite_document(document, user):
        raise DocumentError('Недостаточно прав для работы с избранным.')

    existing = DocumentFavorite.objects.filter(user=user, document=document).first()
    if existing is not None:
        existing.delete()
        is_favorite = False
    else:
        # `get_or_create` and not `create`: the unique constraint is the real
        # guard, and two rapid clicks must not raise at the user.
        DocumentFavorite.objects.get_or_create(user=user, document=document)
        is_favorite = True

    log_event(
        logger,
        'INFO',
        'documents.favorite_toggled',
        document_id=document.pk,
        user_id=getattr(user, 'pk', None),
        is_favorite=is_favorite,
        outcome='ok',
    )
    return is_favorite
