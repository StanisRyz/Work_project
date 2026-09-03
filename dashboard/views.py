"""The landing page: quick access to the sections, plus the user's own tasks.

Reads only. Every answer on it comes from the module that owns it — the cards
from `dashboard/sections.py`, which asks each section's existing permission
rule, and the task rows from `dashboard/selectors.py`, which reuses the task
registry's own queryset and row description. Nothing here is a new permission,
a new query or a new task state.
"""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils import timezone

from .sections import get_quick_access_sections
from .selectors import get_my_active_tasks


@login_required
def dashboard_home(request):
    return render(request, 'dashboard/home.html', {
        'active_page': 'dashboard',
        'header_title': 'Главная',
        'quick_access_sections': get_quick_access_sections(request.user),
        'task_rows': get_my_active_tasks(request.user),
        'today': timezone.localdate(),
    })
