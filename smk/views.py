"""The two СМК pages: creating a record, and reading one back.

Creation is a single structured POST — the record, its findings, its measures
and the real tasks are written together by `smk.services.create_smk_source()`.
The view parses, renders errors and redirects; it decides nothing.

A denial is a 404, exactly as the act and protocol views answer one: an
endpoint must not confirm that something exists to someone who may not use it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import SmkSourceForm
from .permissions import (
    can_create_smk_task,
    can_view_smk_source,
    get_readable_smk_sources_queryset,
)
from .selectors import get_editor_directory, get_source_detail
from .services import SmkWorkflowError, create_smk_source


def _form_context(form):
    return {
        'active_page': 'tasks',
        'header_title': 'Задача СМК',
        'form': form,
        # The prototypes the `<template>` elements render; the browser clones
        # them and renumbers, so no markup is assembled in JavaScript.
        'empty_row': {'index': 0, 'text': '', 'errors': {}},
        'empty_action_row': {
            'index': 0, 'text': '', 'due_date': '', 'assignees': [], 'errors': {},
        },
        'empty_assignee': {'user': '', 'department': ''},
        **get_editor_directory(),
    }


@login_required
def smk_create(request):
    """The СМК task form: source, findings, measures, deadlines, исполнители."""
    if not can_create_smk_task(request.user):
        raise Http404('No SMK source matches the given query.')
    if request.method == 'POST':
        form = SmkSourceForm(request.POST)
        if form.is_valid():
            try:
                source = create_smk_source(
                    origin=form.cleaned['origin'],
                    non_conformities=form.cleaned['non_conformities'],
                    actions=form.cleaned['actions'],
                    created_by=request.user,
                )
            except SmkWorkflowError as exc:
                messages.error(request, str(exc))
                return render(request, 'smk/form.html', _form_context(form), status=400)
            messages.success(request, f'Запись {source.label} создана, задачи назначены.')
            return redirect('smk:detail', pk=source.pk)
        return render(request, 'smk/form.html', _form_context(form), status=400)
    return render(request, 'smk/form.html', _form_context(SmkSourceForm()))


@login_required
def smk_detail(request, pk):
    """The record behind an СМК task, read-only.

    Every authenticated user may open it: the task registry links here, and a
    source a task holder could not read would make the link useless.
    """
    source = get_object_or_404(get_readable_smk_sources_queryset(request.user), pk=pk)
    if not can_view_smk_source(source, request.user):
        raise Http404('No SMK source matches the given query.')
    return render(request, 'smk/detail.html', {
        'active_page': 'tasks',
        'header_title': source.label,
        **get_source_detail(source),
    })
