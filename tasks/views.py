from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .permissions import can_complete_task, get_visible_tasks_queryset
from .services import TaskWorkflowError, complete_task


@login_required
def task_list(request):
    today = timezone.localdate()
    tasks = get_visible_tasks_queryset(request.user).annotate(
        overdue_order=Case(
            When(due_date__lt=today, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by('overdue_order', 'due_date', 'created_at')
    return render(request, 'tasks/list.html', {'active_page': 'tasks', 'tasks': tasks, 'today': today})


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
