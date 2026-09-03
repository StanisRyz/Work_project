"""Read-side state for the СМК pages.

Reads only — every write stays in `smk/services.py`, and every permission in
`smk/permissions.py`.
"""

from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Department

from .models import SmkSource


# The two halves of the registry. «Работа» is every live record — a record
# stays there until somebody archives it by hand, so completing its tasks
# changes nothing here — and «Архив» is what was shelved. The dict is the only
# place the mapping is written, exactly as the protocol registry keeps its own.
LIST_TABS = {
    'work': SmkSource.Status.ACTIVE,
    'archive': SmkSource.Status.ARCHIVED,
}
DEFAULT_LIST_TAB = 'work'


# What a person reads off the record, and never what is stored. `SmkSource`
# keeps exactly two values — «В работе» and «Архив», the shelf it sits on — so
# these four are *derived* every time they are shown: «Архив» once it is
# shelved, «Завершена» when every task its measures produced is closed,
# «Создана» while no task has moved yet, «В работе» in between. Deriving them
# is what keeps this a display concern: nothing here can be out of step with
# the tasks, because it is read from them.
def describe_smk_state(*, is_archived, task_count, completed_task_count):
    """`{'code', 'label'}` for the state pill, from facts the caller counted."""
    if is_archived:
        return {'code': 'archived', 'label': 'Архив'}
    if task_count and completed_task_count == task_count:
        return {'code': 'completed', 'label': 'Завершена'}
    if completed_task_count:
        return {'code': 'in_progress', 'label': 'В работе'}
    return {'code': 'created', 'label': 'Создана'}


def build_smk_list_state(params):
    """The СМК registry for one tab.

    «Количество задач» is counted in the query rather than per row: it is the
    real `tasks.Task` rows the record's measures produced, and one annotation
    keeps the table to a single database read no matter how long it gets.
    """
    tab = params.get('tab') if params else None
    if tab not in LIST_TABS:
        tab = DEFAULT_LIST_TAB
    sources = (
        SmkSource.objects.filter(status=LIST_TABS[tab])
        .select_related('created_by')
        .annotate(
            # Only the measures the record reads by *now*: a correction
            # supersedes the old ones and cancels their tasks, and counting
            # those would leave the registry claiming work nobody holds.
            task_count=Count(
                'actions__tasks',
                filter=Q(actions__superseded_at__isnull=True),
                distinct=True,
            ),
            completed_task_count=Count(
                'actions__tasks',
                filter=Q(
                    actions__superseded_at__isnull=True,
                    actions__tasks__status__code='COMPLETED',
                ),
                distinct=True,
            ),
        )
    )
    # Rows, not the bare queryset: the state pill is derived per record, and
    # deriving it here keeps the template to reading values rather than
    # computing one.
    return {
        'tab': tab,
        'sources': [
            {
                'source': source,
                'task_count': source.task_count,
                'state': describe_smk_state(
                    is_archived=source.is_archived,
                    task_count=source.task_count,
                    completed_task_count=source.completed_task_count,
                ),
            }
            for source in sources
        ],
    }


def get_editor_directory():
    """The department/employee options the form's selectors are built from.

    The same mechanism the protocol editor and the ТО analysis form use: the
    page renders every active employee once, tagged with `data-department-id`,
    and the browser only filters what is already there. No directory endpoint
    is involved, and the server re-checks the department of every submitted
    employee anyway.
    """
    return {
        'departments': Department.objects.filter(is_active=True),
        'employees': User.objects.filter(is_active=True, userprofile__is_active=True)
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username'),
    }


def build_confirmation_summary(cleaned):
    """What the confirmation step shows before anything is written.

    Built from the *validated* structure, never from the raw POST: the numbers
    and names on screen are exactly what `create_smk_source()` would store, so
    a row the form rejected can never be counted in.

    Read-only and side-effect free — it is rendered both into the page (for the
    server-side confirmation step) and, by `smk_form.js`, into the dialog
    without a round trip.
    """
    assignees = []
    for action in cleaned['actions']:
        for user in action['assignees']:
            label = user.get_full_name() or user.username
            if label not in assignees:
                assignees.append(label)
    return {
        'origin_label': SmkSource.Origin(cleaned['origin']).label,
        'audit_date': cleaned['audit_date'],
        'non_conformity_count': len(cleaned['non_conformities']),
        'action_count': len(cleaned['actions']),
        'assignees': assignees,
    }


# The three tabs of the record page. `act` is the default, exactly as the act
# page defaults to its own first tab, and an unknown value falls back to it
# rather than 404ing on a bookmark.
DETAIL_TABS = ('act', 'activities', 'history')


def resolve_detail_tab(value):
    return value if value in DETAIL_TABS else DETAIL_TABS[0]


def _measure_row(action):
    """One корректирующее мероприятие with everything its card shows."""
    # At most one task per measure, by the `unique_smk_action_task` constraint
    # — the card links to that task, never to a search.
    task = next(iter(action.tasks.all()), None)
    return {
        'action': action,
        'assignees': [item.user for item in action.assignees.all()],
        'task': task,
    }


def get_smk_history_groups(source):
    """History events in local-date buckets, newest first.

    The same shape `protocols.selectors.get_protocol_history_groups()` returns,
    so «История» renders through the very same timeline markup the protocol and
    act pages use.
    """
    groups = []
    for event in source.history_events.select_related('actor'):
        event_date = timezone.localtime(event.created_at).date()
        if not groups or groups[-1]['date'] != event_date:
            groups.append({'date': event_date, 'events': []})
        groups[-1]['events'].append(event)
    return groups


def get_cancelled_smk_tasks(source):
    """The record's tasks that a correction withdrew, newest first.

    Read straight off `Task`, not through the measures: a cancelled task hangs
    on a superseded `SmkCorrectiveAction`, and that row is exactly what keeps
    its original wording readable. Nothing is deleted, so this list only grows.
    """
    from tasks.models import Task

    return list(
        Task.objects.filter(
            source_type=Task.SourceType.SMK,
            smk_source=source,
            status__code='CANCELLED',
        )
        .select_related('status', 'smk_action', 'cancelled_by')
        .prefetch_related('assignees__user')
        .order_by('-cancelled_at', '-pk')
    )


def get_source_detail(source):
    """One СМК record: its findings, its measures and the real tasks.

    The tasks are read through `SmkCorrectiveAction.tasks` rather than looked
    up by source type: the relation is what links a measure to the work it
    produced, and each measure has at most one task by database constraint.

    All three tabs are built from this one read, so «Акт аудита», «Связанные
    мероприятия» and «История» can never describe different work.
    """
    rows = [
        _measure_row(action)
        for action in source.current_actions.select_related('department', 'non_conformity')
        .prefetch_related('assignees__user', 'tasks__status')
    ]
    # What «Количество задач» in the information card counts: the real tasks
    # that exist, not the measures that should have produced them.
    task_count = sum(1 for row in rows if row['task'] is not None)
    return {
        'source': source,
        # A finding carries no status of its own: what is being done about it
        # is the state of the measures that name it, and a second answer here
        # could only disagree with them. The wrapper stays so the template
        # keeps one shape to read.
        'non_conformities': [
            {'item': finding} for finding in source.current_non_conformities
        ],
        'actions': rows,
        'task_count': task_count,
        # Derived from the rows already loaded above — the same four states the
        # registry shows, so a record cannot read one way in the list and
        # another on its own page.
        'state': describe_smk_state(
            is_archived=source.is_archived,
            task_count=task_count,
            completed_task_count=sum(
                1 for row in rows
                if row['task'] is not None and row['task'].status.code == 'COMPLETED'
            ),
        ),
        'history_groups': get_smk_history_groups(source),
        # What a correction withdrew, kept on the page rather than only in the
        # task registry: «Связанные мероприятия» is where the work of this
        # record is read, and a cancelled task is part of that story.
        'cancelled_tasks': get_cancelled_smk_tasks(source),
    }
