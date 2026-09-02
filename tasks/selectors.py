"""Shared read-side state for the task registry.

The full page and the live fragment must never answer differently, so both go
through :func:`build_task_list_state`. The builder separates the assigned work
queue from the global authenticated read scope on the server.
"""

from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from .models import Task
from .permissions import get_readable_tasks_queryset, get_visible_tasks_queryset
from .presentation import describe_task


TABS = ('my', 'all', 'archive')
# «Тип задачи», not «Статус». The registry filters on the task's *origin*
# here; the task's own workflow status is a separate column, and conflating
# the two is what this replaces.
SOURCE_TYPE_CHOICES = ('', *Task.SourceType.values)
DUE_CHOICES = ('', 'overdue', 'not_overdue')
SORT_CHOICES = ('', 'nearest', 'farthest')


def _source_search_filter(term):
    """Find a task by the number of the act or protocol behind it.

    Deliberately small: «АОК-2026-00034», «Качество», «Качество №7», «№7» and
    a bare «7» are what people type, and no full-text machinery is involved.
    An explicit «№» splits the term, so a type and a number narrow each other;
    without one the term is tried as a type name or as a number. An СМК record
    is «СМК №4» or a bare number, matched on its own identifier — it has no
    type series of its own to narrow by.
    """
    head, separator, tail = term.partition('№')
    criteria = Q(act__number__icontains=term)
    smk = Q()
    if separator:
        name, number = head.strip(), tail.strip()
        protocol = Q()
        if name:
            protocol &= Q(protocol__protocol_type__name__icontains=name)
        if number.isdigit():
            protocol &= Q(protocol__number=int(number))
        if number.isdigit() and (not name or 'смк' in name.lower()):
            smk = Q(smk_source_id=int(number))
    else:
        protocol = Q(protocol__protocol_type__name__icontains=term)
        if term.isdigit():
            protocol |= Q(protocol__number=int(term))
            smk = Q(smk_source_id=int(term))
    # An empty `Q()` would widen the search to everything instead of adding
    # nothing, so it is never combined in.
    for extra in (protocol, smk):
        if extra:
            criteria |= extra
    return criteria


def build_task_list_state(user, query_params):
    """Return everything the registry needs for `user` and these GET params."""
    today = timezone.localdate()

    tab = query_params.get('tab', 'my')
    if tab not in TABS:
        tab = 'my'
    selected = {
        'number': query_params.get('number', '').strip(),
        'source': query_params.get('source', '').strip(),
        'source_type': query_params.get('source_type', ''),
        'due': query_params.get('due', ''),
        'sort': query_params.get('sort', ''),
    }
    if selected['source_type'] not in SOURCE_TYPE_CHOICES:
        selected['source_type'] = ''
    if selected['due'] not in DUE_CHOICES:
        selected['due'] = ''
    if selected['sort'] not in SORT_CHOICES:
        selected['sort'] = ''

    if tab == 'my':
        tasks = get_visible_tasks_queryset(user)
        tasks = tasks.filter(assignees__user=user)
    else:
        tasks = get_readable_tasks_queryset(user)
        if tab == 'archive':
            tasks = tasks.filter(status__code='COMPLETED')
        else:
            tasks = tasks.exclude(status__code='COMPLETED')
    if tab != 'archive':
        tasks = tasks.exclude(status__code='COMPLETED')

    if selected['number']:
        if selected['number'].isdigit():
            tasks = tasks.filter(pk=int(selected['number']))
        else:
            tasks = tasks.none()
    if selected['source_type']:
        tasks = tasks.filter(source_type=selected['source_type'])
    if selected['source']:
        tasks = tasks.filter(_source_search_filter(selected['source'])).distinct()
    if selected['due'] == 'overdue':
        tasks = tasks.filter(due_date__lt=today)
    elif selected['due'] == 'not_overdue':
        tasks = tasks.filter(due_date__gte=today)

    if selected['sort'] == 'nearest':
        tasks = tasks.order_by('due_date', 'pk')
    elif selected['sort'] == 'farthest':
        tasks = tasks.order_by('-due_date', 'pk')
    elif tab == 'archive':
        tasks = tasks.order_by('-completed_at', 'pk')
    else:
        tasks = tasks.annotate(
            overdue_order=Case(
                When(due_date__lt=today, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
        ).order_by('overdue_order', 'due_date', 'pk')

    tab_urls = {}
    for tab_name in TABS:
        query = query_params.copy()
        query['tab'] = tab_name
        tab_urls[tab_name] = f'?{query.urlencode()}'
    sort_query = query_params.copy()
    sort_query['sort'] = 'farthest' if selected['sort'] == 'nearest' else 'nearest'

    return {
        'tab': tab,
        'selected': selected,
        'tasks': tasks,
        # The rows the templates render: the same queryset, each task paired
        # with its source label, link and real state. Building it here is what
        # keeps the full page and the live fragment identical.
        'rows': [describe_task(task) for task in tasks],
        'source_type_options': Task.SourceType.choices,
        'today': today,
        'tab_urls': tab_urls,
        'reset_url': f'?tab={tab}',
        'sort_url': f'?{sort_query.urlencode()}',
        'list_query': query_params.urlencode(),
    }
