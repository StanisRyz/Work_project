from django.db import transaction
from django.utils import timezone

from references.models import TaskStatus

from .permissions import can_complete_task


class TaskWorkflowError(Exception):
    pass


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
        task.save(update_fields=['status', 'completed_by', 'completed_at', 'execution_comment'])
    return task
