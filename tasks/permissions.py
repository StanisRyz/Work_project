from acts.permissions import has_full_act_access

from .models import Task


def can_view_task(task, user):
    return has_full_act_access(user) or task.assignees.filter(user=user).exists()


def get_visible_tasks_queryset(user):
    tasks = Task.objects.select_related('status', 'act', 'department', 'root_analysis', 'completed_by').prefetch_related('assignees__user')
    return tasks if has_full_act_access(user) else tasks.filter(assignees__user=user).distinct()


def can_complete_task(task, user):
    return task.status.code == 'NEW' and task.assignees.filter(user=user).exists()
