"""The «Мои задачи» block: the user's own open work, shortened.

No task model, queryset or presentation rule of its own. The scope is the one
`tasks.permissions.get_visible_tasks_queryset()` grants, narrowed to the rows
this user is actually assigned to — the same pair the registry's «Мои» tab
applies — and each row is described by `tasks.presentation.describe_task()`, so
the type, the source link and the real status read here exactly as they do in
«Задачи».
"""

from tasks.permissions import get_visible_tasks_queryset
from tasks.presentation import describe_task


# How many rows the block shows, and the reason the page fits one screen: five
# is what the panel holds beside the card grid. A shortcut, not a registry —
# the rest are one click away behind «Перейти ко всем задачам».
TASK_LIMIT = 5


def get_my_active_tasks(user, limit=TASK_LIMIT):
    """The `limit` open tasks assigned to `user` with the nearest deadlines.

    Ordered by `due_date` alone, so the overdue ones — the earliest dates there
    are — come first without a second rule saying so.
    """
    tasks = (
        get_visible_tasks_queryset(user)
        .filter(assignees__user=user)
        # Every task that is over, not only the completed ones: a `CANCELLED`
        # task was withdrawn and must not sit on somebody's dashboard.
        .exclude(status__is_final=True)
        .order_by('due_date', 'pk')
        .distinct()
    )
    return [describe_task(task) for task in tasks[:limit]]
