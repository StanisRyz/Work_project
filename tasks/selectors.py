"""Shared read-side state for the task registry.

The full page and the live fragment must never answer differently, so both go
through :func:`build_task_list_state`. Visibility is decided by the existing
`get_visible_tasks_queryset`, never by the browser.
"""

from django.db.models import Case, IntegerField, Value, When
from django.utils import timezone

from .permissions import get_visible_tasks_queryset


TABS = ('my', 'all', 'archive')
STATUS_CHOICES = ('', 'act')
DUE_CHOICES = ('', 'overdue', 'not_overdue')
SORT_CHOICES = ('', 'nearest', 'farthest')


def build_task_list_state(user, query_params):
    """Return everything the registry needs for `user` and these GET params."""
    today = timezone.localdate()

    tab = query_params.get('tab', 'my')
    if tab not in TABS:
        tab = 'my'
    selected = {
        'number': query_params.get('number', '').strip(),
        'source': query_params.get('source', '').strip(),
        'status': query_params.get('status', ''),
        'due': query_params.get('due', ''),
        'sort': query_params.get('sort', ''),
    }
    if selected['status'] not in STATUS_CHOICES:
        selected['status'] = ''
    if selected['due'] not in DUE_CHOICES:
        selected['due'] = ''
    if selected['sort'] not in SORT_CHOICES:
        selected['sort'] = ''

    tasks = get_visible_tasks_queryset(user)
    if tab == 'my':
        tasks = tasks.filter(assignees__user=user)
    elif tab == 'archive':
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
    if selected['source']:
        tasks = tasks.filter(act__number__icontains=selected['source'])
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
        'today': today,
        'tab_urls': tab_urls,
        'reset_url': f'?tab={tab}',
        'sort_url': f'?{sort_query.urlencode()}',
        'list_query': query_params.urlencode(),
    }
