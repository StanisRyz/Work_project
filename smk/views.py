"""The СМК pages: the registry, creating a record, and reading one back.

Creation is a two-step POST to one endpoint: a valid submission first comes
back as a confirmation of what would be created, and only a submission
carrying the confirmation flag writes anything. What it then writes — the
record, its findings, its measures and the real tasks — is written together by
`smk.services.create_smk_source()`. The view parses, confirms, renders errors
and redirects; it decides nothing else.

Archiving is the record's only state change and lives on its own POST-only
endpoint; the registry is a plain two-tab read.

A denial is a 404, exactly as the act and protocol views answer one: an
endpoint must not confirm that something exists to someone who may not use it.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .forms import SmkSourceForm
from .permissions import (
    can_archive_smk_source,
    can_create_smk_task,
    can_view_smk_source,
    get_readable_smk_sources_queryset,
)
from .selectors import (
    build_confirmation_summary,
    build_smk_list_state,
    get_editor_directory,
    get_source_detail,
    resolve_detail_tab,
)
from .services import SmkWorkflowError, archive_smk_source, create_smk_source


# The one value the POST must carry for anything to be written. A flow flag,
# not a field of the record, so it is read here and never by `SmkSourceForm`.
CONFIRMATION_FIELD = 'confirmed'
CONFIRMATION_VALUE = '1'


def _form_context(form, confirmation=None):
    return {
        'active_page': 'smk',
        'header_title': 'Задача СМК',
        'form': form,
        # Present only on the confirmation step; the template renders the
        # dialog open when it is, so the step exists without JavaScript too.
        'confirmation': confirmation,
        'confirmation_field': CONFIRMATION_FIELD,
        'confirmation_value': CONFIRMATION_VALUE,
        # The prototypes the `<template>` elements render; the browser clones
        # them and renumbers, so no markup is assembled in JavaScript.
        'empty_row': {'index': 0, 'text': '', 'errors': {}},
        'empty_action_row': {
            'index': 0, 'text': '', 'due_date': '', 'requires_attachment': False,
            'assignees': [], 'errors': {},
        },
        'empty_assignee': {'user': '', 'department': ''},
        **get_editor_directory(),
    }


@login_required
def smk_list(request):
    """The СМК registry, in the two tabs «Работа» and «Архив».

    Reading is open to every authenticated user, exactly as the record page is;
    what a user without the right loses is «Создать», not the list.
    """
    return render(request, 'smk/list.html', {
        'active_page': 'smk',
        'header_title': 'СМК',
        'can_create': can_create_smk_task(request.user),
        **build_smk_list_state(request.GET),
    })


@login_required
def smk_create(request):
    """The СМК task form: source, findings, measures, deadlines, исполнители.

    Two steps, both on this one endpoint. A valid POST without the
    confirmation flag writes **nothing**: it comes back as the same page with
    the summary of what would be created, and only a POST carrying the flag
    reaches `create_smk_source()`. The dialog in the browser is the fast path
    to that second POST, never the guarantee — the guarantee is this check,
    which a request bypassing the page cannot skip either.
    """
    if not can_create_smk_task(request.user):
        raise Http404('No SMK source matches the given query.')
    if request.method == 'POST':
        form = SmkSourceForm(request.POST)
        if form.is_valid():
            if request.POST.get(CONFIRMATION_FIELD) != CONFIRMATION_VALUE:
                return render(request, 'smk/form.html', _form_context(
                    form, build_confirmation_summary(form.cleaned),
                ))
            try:
                source = create_smk_source(
                    origin=form.cleaned['origin'],
                    audit_date=form.cleaned['audit_date'],
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
    """The record behind an СМК task, read-only, in two tabs.

    «Акт аудита» is the record itself — findings and the measures answering
    them; «Связанные мероприятия» is the work it created, in the same table
    shape the act page uses. There is no history tab: an СМК record has no
    workflow, so there would be no events to show.

    Every authenticated user may open it: the task registry links here, and a
    source a task holder could not read would make the link useless.
    """
    source = get_object_or_404(get_readable_smk_sources_queryset(request.user), pk=pk)
    if not can_view_smk_source(source, request.user):
        raise Http404('No SMK source matches the given query.')
    return render(request, 'smk/detail.html', {
        'active_page': 'smk',
        'header_title': source.label,
        # Asked once, here: the button and the POST that follows it read the
        # very same answer, so one can never be offered without the other.
        'can_archive': can_archive_smk_source(source, request.user),
        # Which tab, read from the query string exactly as the act page reads
        # its own. An unknown value falls back to «Акт аудита» rather than
        # answering 404 to a stale bookmark.
        'detail_tab': resolve_detail_tab(request.GET.get('tab', '')),
        **get_source_detail(source),
    })


@login_required
@require_POST
def smk_archive(request, pk):
    """«Архивировать» — POST only, and the record's one state change.

    The view confirms nothing itself: the modal in the browser is the fast path
    to this POST, and `archive_smk_source()` re-checks the right under a lock.
    A refusal comes back as a message on the record, which stays readable —
    archiving hides nothing.
    """
    source = get_object_or_404(get_readable_smk_sources_queryset(request.user), pk=pk)
    if not can_view_smk_source(source, request.user):
        raise Http404('No SMK source matches the given query.')
    try:
        archive_smk_source(source, actor=request.user)
    except SmkWorkflowError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Запись {source.label} перенесена в архив.')
    return redirect('smk:detail', pk=source.pk)
