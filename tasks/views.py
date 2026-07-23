from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Value, When
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from .permissions import get_visible_tasks_queryset


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
    return render(request, 'tasks/detail.html', {'active_page': 'tasks', 'task': task, 'today': timezone.localdate()})
