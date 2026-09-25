"""Read-side state for the СМК pages.

Reads only — every write stays in `smk/services.py`, and every permission in
`smk/permissions.py`.
"""

from django.contrib.auth.models import User
from django.db.models import Count, Q
from django.utils import timezone

from accounts.models import Department

from .models import SmkSource


# The three halves of the registry. «Работа» is a live record with work still
# open, «Выполнено» a live record whose every task is done — the shelf it can
# be archived from — and «Архив» what somebody shelved by hand. Only the last
# is a stored `status`; the first two are one stored status split by the state
# of the tasks, which is why this is a tuple of tab names rather than the
# status map it used to be.
LIST_TABS = ('work', 'completed', 'archive')
DEFAULT_LIST_TAB = 'work'


# The record's live tasks, counted two ways. Both read only the measures the
# record holds by *now*: a correction supersedes the old ones and cancels their
# tasks, and counting those would let a withdrawn task speak for work nobody
# holds. Both filters are positive — «выполнена» and «ещё не закрыта» — so a
# `CANCELLED` task lands in neither, which is exactly «отменённые не
# учитываются»: it is not outstanding work, and it is not done work either.
_COMPLETED_TASKS = Count(
    'actions__tasks',
    filter=Q(
        actions__superseded_at__isnull=True,
        actions__tasks__status__code='COMPLETED',
    ),
    distinct=True,
)
_OPEN_TASKS = Count(
    'actions__tasks',
    filter=Q(
        actions__superseded_at__isnull=True,
        actions__tasks__status__is_final=False,
    ),
    distinct=True,
)


def is_task_set_completed(tasks):
    """Whether this set of live tasks means the record is «Выполнено».

    The one definition, shared by the record page and — restated as the two
    annotations above, which must keep agreeing with it — by the registry.

    `done > 0` is not a formality: a record whose only task was cancelled has
    an empty «выполненные» set, and «все выполнены» over nothing would report
    finished work that never happened.
    """
    done = 0
    for task in tasks:
        if task.status.code == 'COMPLETED':
            done += 1
        elif not task.status.is_final:
            return False
    return done > 0


# What a person reads off the record. «Архивировано» is the one stored answer
# and wins over everything: a shelved record is read as shelved whatever its
# tasks say. The other two are derived from the work itself — «Выполнено» the
# moment every live task is done, «В работе» until then — so the pill can never
# claim work that is still open, and it goes back on its own if a task is
# returned to work.
def describe_smk_state(*, is_archived, is_completed=False):
    """`{'code', 'label'}` for the state pill. `code` drives `.status-badge--*`."""
    if is_archived:
        return {'code': 'archived', 'label': 'Архивировано'}
    if is_completed:
        return {'code': 'completed', 'label': 'Выполнено'}
    return {'code': 'in_progress', 'label': 'В работе'}


def _registry_tasks(source):
    """The record's live tasks, in deadline order, for one registry row.

    What the arrow next to «Срок выполнения» expands into, and — through
    `_next_due_date()` below — where the срок itself comes from, so the column
    and the list it opens can never name different work.

    Only the measures the record holds by now, and `CANCELLED` tasks left out:
    a задача withdrawn by a correction is neither outstanding work nor done
    work, exactly as the two count annotations above already treat it. Read
    from the prefetched relations rather than queried, so a hundred rows still
    cost the same two reads as one.
    """
    tasks = [
        task
        for action in source.actions.all()
        if action.superseded_at is None
        for task in action.tasks.all()
        if task.status.code != 'CANCELLED'
    ]
    return sorted(tasks, key=lambda task: (task.due_date, task.pk))


def _next_due_date(tasks):
    """«Срок выполнения»: the nearest задача that is still open.

    The first one by deadline whose status is not final — so closing it moves
    the column on to the next by itself, and a record with nothing outstanding
    shows no срок at all rather than the last date it happened to hold.
    """
    for task in tasks:
        if not task.status.is_final:
            return task.due_date
    return None


def _in_tab(sources, tab):
    """The records of one registry tab."""
    if tab == 'archive':
        return sources.filter(status=SmkSource.Status.ARCHIVED)
    # The same predicate `is_task_set_completed()` states, expressed over the
    # annotations: done work exists and nothing is still open. `work` is its
    # complement within the live records, so no record can fall between the
    # two tabs or appear in both.
    done = Q(completed_task_count__gt=0, open_task_count=0)
    live = sources.filter(status=SmkSource.Status.ACTIVE)
    return live.filter(done) if tab == 'completed' else live.exclude(done)


def build_smk_list_state(params):
    """The СМК registry for one tab.

    Every count is an annotation rather than a per-row read: the table stays a
    single database query no matter how long it gets, and «Выполнено» is
    decided by the same query that fetches the rows. «Срок выполнения» and the
    list the arrow expands are read from one prefetch of the same tasks, which
    is two reads for the whole table however long it is.
    """
    params = params or {}
    tab = params.get('tab')
    if tab not in LIST_TABS:
        tab = DEFAULT_LIST_TAB
    # Filters: the audit's kind, the отдел it looked at and its year. Each is
    # checked against what it may be, so an unknown value is «Все», never an
    # error and never a query on something the page does not offer.
    selected = {
        'origin': params.get('origin', ''),
        'department': params.get('department', ''),
        'year': params.get('year', ''),
    }
    if selected['origin'] not in SmkSource.Origin.values:
        selected['origin'] = ''
    if not str(selected['department']).isdigit():
        selected['department'] = ''
    if not (str(selected['year']).isdigit() and len(str(selected['year'])) == 4):
        selected['year'] = ''
    sources = (
        SmkSource.objects.select_related('created_by', 'department')
        .prefetch_related('actions__tasks__status')
        .annotate(
            # «Задач» for the table's own column, and the two the state is read
            # from. All three are `distinct=True`: three counts over the same
            # multi-valued join would otherwise multiply each other's rows.
            task_count=Count(
                'actions__tasks',
                filter=Q(actions__superseded_at__isnull=True),
                distinct=True,
            ),
            completed_task_count=_COMPLETED_TASKS,
            open_task_count=_OPEN_TASKS,
        )
    )
    if selected['origin']:
        sources = sources.filter(origin=selected['origin'])
    if selected['department']:
        sources = sources.filter(department_id=int(selected['department']))
    if selected['year']:
        sources = sources.filter(audit_date__year=int(selected['year']))
    tab_querysets = {name: _in_tab(sources, name) for name in LIST_TABS}
    sources = tab_querysets[tab]
    # Rows, not the bare queryset: the state pill is derived per record, and
    # deriving it here keeps the template to reading values rather than
    # computing one.
    all_sources = SmkSource.objects.all()
    return {
        'tab': tab,
        'selected': selected,
        'has_filters': any(selected.values()),
        # How many records each tab holds under the filters the page shows.
        'tab_counts': {name: queryset.count() for name, queryset in tab_querysets.items()},
        'origin_options': SmkSource.Origin.choices,
        # Only the отделы and years some record actually names: a filter that
        # can only ever answer «ничего не найдено» is not an option.
        'department_options': Department.objects.filter(
            pk__in=all_sources.exclude(department=None).values('department')
        ).order_by('name'),
        'year_options': sorted(
            {day.year for day in all_sources.exclude(audit_date=None).values_list('audit_date', flat=True)},
            reverse=True,
        ),
        # The one «сегодня» every row's overdue mark is compared against, so a
        # long table cannot straddle midnight and read two different answers.
        'today': timezone.localdate(),
        'sources': [
            {
                'source': source,
                'task_count': source.task_count,
                # «3 из 5»: done against done-or-open, the same two annotations
                # the state is read from — a withdrawn task is in neither.
                'progress': {
                    'done': source.completed_task_count,
                    'total': source.completed_task_count + source.open_task_count,
                },
                'tasks': tasks,
                'next_due_date': _next_due_date(tasks),
                'state': describe_smk_state(
                    is_archived=source.is_archived,
                    is_completed=(
                        source.completed_task_count > 0
                        and source.open_task_count == 0
                    ),
                ),
            }
            for source, tasks in (
                (source, _registry_tasks(source)) for source in sources
            )
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
        'department_label': str(cleaned['department']),
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
    """One корректирующее мероприятие with everything its card shows.

    One task or several: a measure produces one shared task, or — with
    «Разбить задачу по исполнителям» — one per исполнитель, exactly as a
    protocol decision does. The rows are read from the measure's own `tasks`
    relation, so the page can never show work that belongs to another measure.
    """
    tasks = sorted(action.tasks.all(), key=lambda task: task.pk)
    return {
        'action': action,
        'assignees': [item.user for item in action.assignees.all()],
        'tasks': tasks,
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
    on the superseded `SmkCorrectiveAction` it was issued from, and that row is
    exactly what keeps its original wording readable. A measure the correction
    left alone has no cancelled task, so nothing an исполнитель still holds can
    appear here. Nothing is deleted, so this list only grows.
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
        .prefetch_related(
            'assignees__user', 'tasks__status', 'tasks__individual_assignee',
        )
    ]
    # What «Количество задач» in the information card counts: the real tasks
    # that exist, not the measures that should have produced them. A split
    # measure contributes one per исполнитель, which is what a reader counts on
    # the «Связанные мероприятия» tab.
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
        'task_count': sum(len(row['tasks']) for row in rows),
        # The same three states the registry shows, from the same function and
        # the same rule, so a record cannot read one way in the list and
        # another on its own page. The tasks are already loaded above, so
        # «Выполнено» costs no extra query here.
        'state': describe_smk_state(
            is_archived=source.is_archived,
            is_completed=is_task_set_completed(
                [task for row in rows for task in row['tasks']]
            ),
        ),
        'history_groups': get_smk_history_groups(source),
        # What a correction withdrew, kept on the page rather than only in the
        # task registry: «Связанные мероприятия» is where the work of this
        # record is read, and a cancelled task is part of that story.
        'cancelled_tasks': get_cancelled_smk_tasks(source),
    }
