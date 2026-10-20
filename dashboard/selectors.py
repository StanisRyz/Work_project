"""The landing page's lists: the user's own open work, shortened.

No task model, queryset or presentation rule of its own. The scope is the one
`tasks.permissions.get_visible_tasks_queryset()` grants, narrowed to the rows
this user is actually assigned to — the same pair the registry's «Мои» tab
applies — and each row is described by `tasks.presentation.describe_task()`, so
the type, the source link and the real status read here exactly as they do in
«Задачи».
"""

from django.db.models import F

from acts.permissions import get_visible_acts_queryset
from protocols.models import Protocol, ProtocolApproval
from tasks.permissions import get_visible_tasks_queryset
from tasks.presentation import describe_task


# How many rows each list shows. Shortcuts, not registries — the rest are one
# click away behind the list's own «Все …» link, and with these the page still
# fits one 1080 px screen.
TASK_LIMIT = 8
ACT_LIMIT = 6
APPROVAL_LIMIT = 5


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


def get_my_active_acts(user, limit=ACT_LIMIT):
    """The «Мои акты» work queue, nearest deadline first.

    The very queryset the registry's first tab and the «Мои акты в работе»
    count read (`get_visible_acts_queryset()` without the archive), so the
    list, the number above it and the registry agree.
    """
    acts = (
        get_visible_acts_queryset(user)
        .exclude(status__code='ARCHIVED')
        .select_related('status')
        .order_by('due_date', 'pk')
    )
    return list(acts[:limit])


def get_my_pending_approvals(user, limit=APPROVAL_LIMIT):
    """The protocols waiting for this user's signature, oldest round first.

    The `ProtocolApproval` rows `dashboard.summary.my_work_counts()` counts:
    still `PENDING`, on the *current* revision of a protocol in approval —
    exactly the ones its page offers «Согласовать» on.
    """
    approvals = (
        ProtocolApproval.objects.filter(
            user=user,
            status=ProtocolApproval.Status.PENDING,
            protocol__status=Protocol.Status.APPROVAL,
            revision=F('protocol__revision'),
        )
        .select_related('protocol__protocol_type', 'protocol__author', 'task')
        .order_by('created_at', 'pk')
    )
    # The approval row itself: its protocol, and its queue task's deadline.
    return list(approvals[:limit])
