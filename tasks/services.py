from django.db import transaction
from django.utils import timezone

from realtime.emitters import emit_task_completed, emit_task_updated
from references.models import TaskStatus

from .models import TaskAssignee
from .permissions import can_complete_task


class TaskWorkflowError(Exception):
    pass


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
        raise TaskWorkflowError('У задачи должен остаться хотя бы один исполнитель.')

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
    return task


def complete_task(task, user, execution_comment):
    """Complete one shared task once, on behalf of one assigned employee."""
    with transaction.atomic():
        task = task.__class__.objects.select_for_update().prefetch_related('assignees').get(pk=task.pk)
        if not can_complete_task(task, user):
            raise TaskWorkflowError('Завершение задачи недоступно.')
        execution_comment = (execution_comment or '').strip()
        if not execution_comment:
            raise TaskWorkflowError('Укажите результат выполнения задачи.')
        try:
            completed_status = TaskStatus.objects.get(code='COMPLETED', is_active=True)
        except TaskStatus.DoesNotExist as exc:
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
    return task
