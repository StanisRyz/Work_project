import logging
import time

from django.db import transaction
from django.utils import timezone

from ecosystem.logging_utils import log_event
from realtime.emitters import emit_task_completed, emit_task_updated
from references.models import TaskStatus

from .models import Task, TaskAssignee
from .permissions import can_complete_task


logger = logging.getLogger('ecosystem.workflow')


class TaskWorkflowError(Exception):
    pass


def _pk_of(value):
    return getattr(value, 'pk', value if isinstance(value, int) else None)


def replace_task_assignees(task, users, *, actor=None):
    """Set a saved task's assignees to exactly `users`, atomically.

    The single place assignments may change. There is no UI for editing a
    task's assignees yet, so this exists as the safe extension point rather
    than as a feature: any future editing screen must call this instead of
    touching `TaskAssignee` directly, and no signal is involved.

    Bumping `Task.updated_at` is the whole point. Assignments live in a child
    table, so writing them leaves the task row — and therefore the real-time
    tasks revision derived from it — untouched unless the parent is saved
    explicitly. `actor` is accepted for symmetry with the other services and
    for future history/audit use; it is not a permission check.
    """
    target_ids = sorted({user.pk if hasattr(user, 'pk') else int(user) for user in users})
    if not target_ids:
        log_event(
            logger,
            'INFO',
            'task.operation_rejected',
            operation='replace_assignees',
            task_id=_pk_of(task),
            actor_user_id=_pk_of(actor),
            reason='no_assignees',
            outcome='rejected',
        )
        raise TaskWorkflowError('У задачи должен остаться хотя бы один исполнитель.')

    started = time.monotonic()
    with transaction.atomic():
        task = task.__class__.objects.select_for_update().get(pk=task.pk)
        current_ids = set(
            TaskAssignee.objects.filter(task=task).values_list('user_id', flat=True)
        )
        removed = current_ids - set(target_ids)
        added = [user_id for user_id in target_ids if user_id not in current_ids]
        if not removed and not added:
            return task

        if removed:
            TaskAssignee.objects.filter(task=task, user_id__in=removed).delete()
        TaskAssignee.objects.bulk_create(
            [TaskAssignee(task=task, user_id=user_id) for user_id in added]
        )
        # Explicit, never a signal: `updated_at` is `auto_now=True`, which
        # Django only applies to fields listed in `update_fields`.
        task.save(update_fields=['updated_at'])
        emit_task_updated(task, changed_fields=('assignees',))
    # Counts only: who was added or removed is in the task's own records, and
    # a log file is not the place for a list of employees.
    log_event(
        logger,
        'INFO',
        'task.assignees_replaced',
        task_id=task.pk,
        act_id=task.act_id,
        actor_user_id=_pk_of(actor),
        assignee_count=len(target_ids),
        added_count=len(added),
        removed_count=len(removed),
        duration_ms=(time.monotonic() - started) * 1000,
        outcome='ok',
    )
    return task


def complete_task(task, user, execution_comment):
    """Complete one shared task once, on behalf of one assigned employee."""
    started = time.monotonic()

    def _rejected(reason, previous_status=None):
        # A second completion, a lost race or a missing right — an ordinary
        # outcome, recorded by reason code. The execution comment is the
        # employee's own text and is never logged.
        log_event(
            logger,
            'INFO',
            'task.operation_rejected',
            operation='complete',
            task_id=_pk_of(task),
            act_id=getattr(task, 'act_id', None),
            actor_user_id=_pk_of(user),
            previous_status=previous_status,
            reason=reason,
            duration_ms=(time.monotonic() - started) * 1000,
            outcome='rejected',
        )

    with transaction.atomic():
        task = task.__class__.objects.select_for_update().prefetch_related('assignees').get(pk=task.pk)
        previous_status = getattr(task.status, 'code', None)
        # Stated separately from the permission check so the refusal is
        # legible in the log: this is not "you may not", it is "this kind of
        # task is not finished this way". `can_complete_task` refuses it too.
        if task.source_type == Task.SourceType.PROTOCOL_APPROVAL:
            _rejected('protocol_approval_not_completable', previous_status)
            raise TaskWorkflowError('Задача согласования протокола не завершается таким образом.')
        if not can_complete_task(task, user):
            _rejected('not_permitted_or_already_completed', previous_status)
            raise TaskWorkflowError('Завершение задачи недоступно.')
        execution_comment = (execution_comment or '').strip()
        if not execution_comment:
            _rejected('empty_execution_comment', previous_status)
            raise TaskWorkflowError('Укажите результат выполнения задачи.')
        try:
            completed_status = TaskStatus.objects.get(code='COMPLETED', is_active=True)
        except TaskStatus.DoesNotExist as exc:
            log_event(
                logger,
                'ERROR',
                'task.operation_failed',
                operation='complete',
                task_id=_pk_of(task),
                act_id=getattr(task, 'act_id', None),
                actor_user_id=_pk_of(user),
                previous_status=previous_status,
                error_type='MissingCompletedTaskStatus',
                outcome='failed',
            )
            raise TaskWorkflowError('Не найден активный статус задачи «Выполнено».') from exc
        task.status = completed_status
        task.completed_by = user
        task.completed_at = timezone.now()
        task.execution_comment = execution_comment
        # `updated_at` is `auto_now=True`, but Django only bumps an auto_now
        # field when it is explicitly listed in `update_fields` — omitting it
        # here would silently leave the timestamp (and the sync revision token
        # derived from it) unchanged despite the field being auto_now.
        task.save(
            update_fields=['status', 'completed_by', 'completed_at', 'execution_comment', 'updated_at']
        )
        # Inside the lock: a second, parallel completion is refused by
        # `can_complete_task` above and never reaches this line.
        emit_task_completed(task)
    log_event(
        logger,
        'INFO',
        'task.completed',
        task_id=task.pk,
        act_id=task.act_id,
        actor_user_id=_pk_of(user),
        assignee_count=len(task.assignees.all()),
        previous_status=previous_status,
        next_status=completed_status.code,
        duration_ms=(time.monotonic() - started) * 1000,
        outcome='ok',
    )
    return task
