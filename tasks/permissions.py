from acts.permissions import has_full_act_access

from .models import Task


def can_view_task(task, user):
    return has_full_act_access(user) or task.responsible_id == getattr(user, 'id', None)


def get_visible_tasks_queryset(user):
    tasks = Task.objects.select_related('status', 'act', 'department', 'responsible', 'root_analysis')
    return tasks if has_full_act_access(user) else tasks.filter(responsible=user)
