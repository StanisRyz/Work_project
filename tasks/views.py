from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from realtime.auth import realtime_login_required

from .permissions import can_complete_task, get_readable_tasks_queryset, get_visible_tasks_queryset
from .selectors import build_task_list_state
from .services import TaskWorkflowError, complete_task


@login_required
def task_list(request):
    state = build_task_list_state(request.user, request.GET)
    return render(request, 'tasks/list.html', {
        'active_page': 'tasks', 'header_title': 'Задачи', **state,
    })


@realtime_login_required
@require_GET
def task_list_fragment(request):
    """Current registry results for the live client.

    Same builder, same partial and the same query parameters as the full page,
    so what the browser swaps in is exactly what a reload would render. The
    recipient is always `request.user`: no user parameter is accepted, and a
    GET never changes anything.
    """
    state = build_task_list_state(request.user, request.GET)
    results_html = render_to_string(
        'tasks/includes/list_results.html', state, request=request
    )
    response = JsonResponse(
        {
            'results_html': results_html,
            'tab': state['tab'],
            'task_ids': list(state['tasks'].values_list('pk', flat=True)),
            'generated_at': timezone.now().isoformat(),
        }
    )
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


@login_required
def task_detail(request, pk):
    task = get_object_or_404(get_readable_tasks_queryset(request.user), pk=pk)
    context = _task_detail_context(task, request.user, request.GET.urlencode())
    context['header_title'] = f'Задача {task.pk}'
    return render(request, 'tasks/detail.html', context)


def _task_detail_context(task, user, list_query='', execution_comment='', execution_error=''):
    return {
        'active_page': 'tasks', 'header_title': f'Задача {task.pk}', 'task': task, 'today': timezone.localdate(),
        'can_complete': can_complete_task(task, user), 'list_query': list_query,
        'execution_comment': execution_comment, 'execution_error': execution_error,
    }


@login_required
def complete_task_view(request, pk):
    if request.method != 'POST':
        return redirect('tasks:detail', pk=pk)
    task = get_object_or_404(get_visible_tasks_queryset(request.user), pk=pk)
    execution_comment = request.POST.get('execution_comment', '')
    list_query = request.POST.get('list_query', '')
    try:
        complete_task(task, request.user, execution_comment)
    except TaskWorkflowError as exc:
        return render(
            request, 'tasks/detail.html',
            _task_detail_context(task, request.user, list_query, execution_comment, str(exc)), status=400,
        )
    return redirect(f"{reverse('tasks:list')}?tab=archive&number={task.pk}")
