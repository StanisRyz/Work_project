from acts.permissions import has_full_act_access, is_act_admin

from .models import Task


# The source relations are nullable, so every one of them is LEFT JOINed here:
# selecting them is safe for a task that has none, and the columns simply come
# back as NULL. Read-side code must therefore never assume `task.act` or
# `task.root_analysis` is present — ask `task.source_type` instead.
_SOURCE_AWARE_SELECT_RELATED = (
    'status', 'department', 'completed_by', 'act', 'root_analysis',
    # The protocol type carries the label «Качество №7», and the reverse
    # one-to-one `protocol_approval` carries the real outcome of an approval
    # queue entry; both are read for every registry row.
    'protocol__protocol_type', 'protocol_action', 'protocol_approval',
)


def can_view_task(task, user):
    return bool(getattr(user, 'is_authenticated', False))


def _tasks_queryset():
    return Task.objects.select_related(*_SOURCE_AWARE_SELECT_RELATED).prefetch_related(
        'assignees__user__userprofile'
    )


def get_visible_tasks_queryset(user):
    """Tasks in the user's working scope."""
    tasks = _tasks_queryset()
    return tasks if has_full_act_access(user) else tasks.filter(assignees__user=user).distinct()


def get_readable_tasks_queryset(user):
    """All tasks readable by an authenticated user."""
    tasks = _tasks_queryset()
    return tasks if getattr(user, 'is_authenticated', False) else tasks.none()


def can_complete_task(task, user):
    """Who may finish this task through the ordinary completion flow.

    A protocol approval task is never completable here: agreeing to a protocol
    is its own decision with its own endpoint, and closing it by posting an
    execution result would silently approve a document. No such task exists
    yet — this is the invariant the later stage builds on, stated once, in the
    one place both the view and the service ask.
    """
    if task.source_type == Task.SourceType.PROTOCOL_APPROVAL:
        return False
    return task.status.code == 'IN_PROGRESS' and (
        is_act_admin(user) or task.assignees.filter(user=user).exists()
    )
