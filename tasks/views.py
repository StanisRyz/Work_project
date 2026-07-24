from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .permissions import can_complete_task, get_visible_tasks_queryset
from .services import TaskWorkflowError, complete_task


@login_required
def task_list(request):
    today = timezone.localdate()
    tab = request.GET.get('tab', 'my')
    if tab not in {'my', 'all', 'archive'}:
        tab = 'my'
    selected = {
        'number': request.GET.get('number', '').strip(),
        'source': request.GET.get('source', '').strip(),
        'status': request.GET.get('status', ''),
        'due': request.GET.get('due', ''),
        'sort': request.GET.get('sort', ''),
    }
    if selected['status'] not in {'', 'act'}:
        selected['status'] = ''
    if selected['due'] not in {'', 'overdue', 'not_overdue'}:
        selected['due'] = ''
    if selected['sort'] not in {'', 'nearest', 'farthest'}:
        selected['sort'] = ''

    tasks = get_visible_tasks_queryset(request.user)
    if tab == 'my':
        tasks = tasks.filter(assignees__user=request.user)
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
                default=Value(1), output_field=IntegerField(),
            )
        ).order_by('overdue_order', 'due_date', 'pk')

    tab_urls = {}
    for tab_name in ('my', 'all', 'archive'):
        query = request.GET.copy()
        query['tab'] = tab_name
        tab_urls[tab_name] = f'?{query.urlencode()}'
    sort_query = request.GET.copy()
    sort_query['sort'] = 'farthest' if selected['sort'] == 'nearest' else 'nearest'
    sort_url = f'?{sort_query.urlencode()}'
    return render(request, 'tasks/list.html', {
        'active_page': 'tasks', 'header_title': 'Задачи', 'tasks': tasks, 'today': today, 'tab': tab,
        'selected': selected, 'tab_urls': tab_urls, 'reset_url': f'?tab={tab}', 'sort_url': sort_url,
    })


@login_required
def task_detail(request, pk):
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    return render(request, 'tasks/detail.html', {
        'active_page': 'tasks', 'task': task, 'today': timezone.localdate(),
        'can_complete': can_complete_task(task, request.user),
    })


@login_required
def complete_task_view(request, pk):
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    try:
        complete_task(task, request.user)
    except TaskWorkflowError:
        return redirect('tasks:detail', pk=pk)
    return redirect('tasks:detail', pk=pk)
