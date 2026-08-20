"""Protocols pages: the registry, type selection and the single-page editor.

Thin by design. A view parses the request, asks `protocols/permissions.py` who
is allowed to do what, hands the parsed structure to `protocols/services.py`
and renders — no protocol content is written here.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ProtocolDraftForm
from .models import Protocol, ProtocolType
from .permissions import (
    can_decide_protocol_approval,
    can_delete_draft_protocol,
    can_edit_protocol,
    can_send_protocol_for_approval,
)
from .selectors import (
    build_protocol_list_state,
    get_active_protocol_types,
    get_approval_progress,
    get_approval_revision_groups,
    get_current_approval_rows,
    get_editor_directory,
    get_protocol_history_groups,
    get_readable_protocols_queryset,
    get_user_approval,
)
from .services import (
    ProtocolWorkflowError,
    approve_protocol,
    create_protocol,
    delete_draft_protocol,
    return_protocol_for_revision,
    save_protocol_draft,
    send_protocol_for_approval,
)


@login_required
def protocol_list(request):
    state = build_protocol_list_state(request.GET)
    return render(request, 'protocols/list.html', {
        'active_page': 'protocols', 'header_title': 'Протоколы', **state,
    })


@login_required
def protocol_create(request):
    """Choose a type; the service does the numbering, the author row and history."""
    protocol_types = get_active_protocol_types()
    if request.method == 'POST':
        protocol_type = protocol_types.filter(pk=_as_int(request.POST.get('protocol_type'))).first()
        if protocol_type is None:
            return render(request, 'protocols/create.html', {
                'active_page': 'protocols', 'header_title': 'Создание протокола',
                'protocol_types': protocol_types, 'error': 'Выберите тип протокола.',
            }, status=400)
        protocol = create_protocol(protocol_type, request.user)
        return redirect('protocols:detail', pk=protocol.pk)
    return render(request, 'protocols/create.html', {
        'active_page': 'protocols', 'header_title': 'Создание протокола',
        'protocol_types': protocol_types, 'error': '',
    })


@login_required
def protocol_detail(request, pk):
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    return render(request, 'protocols/detail.html', _detail_context(request, protocol))


@login_required
def protocol_save_draft(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    # The permission is enforced here and again inside the service under the
    # row lock; hiding the button is presentation, never the check.
    if not can_edit_protocol(protocol, request.user):
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error='Изменить этот протокол нельзя.'),
            status=403,
        )
    form = ProtocolDraftForm(protocol, request.POST)
    if not form.is_valid():
        return render(
            request, 'protocols/detail.html', _detail_context(request, protocol, form=form),
            status=400,
        )
    try:
        save_protocol_draft(protocol, request.user, form.cleaned)
    except ProtocolWorkflowError as exc:
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Черновик протокола сохранён.')
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_delete(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        delete_draft_protocol(protocol, request.user)
    except ProtocolWorkflowError as exc:
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Черновик протокола удалён.')
    return redirect('protocols:list')


# --------------------------------------------------------------------------
# Workflow endpoints
#
# POST only, one per transition, and each one a thin wrapper: the view parses
# the request, asks `protocols/permissions.py` the presentation question and
# hands the decision to `protocols/services.py`, which re-checks status, actor
# and content under the row lock. No state machine is restated here, and a GET
# never reaches a service — it redirects to the protocol page.
# --------------------------------------------------------------------------


@login_required
def protocol_send_for_approval(request, pk):
    """Save what the editor currently shows, then submit that content.

    The two steps are deliberately separate calls in that order: the author
    presses one button, but what goes for approval must be exactly what they
    are looking at, so the visible form is validated and stored first and the
    workflow service then re-reads the persisted protocol. An invalid form
    submits nothing; a draft that saves but is refused by
    `validate_protocol_for_approval()` leaves the protocol editable with the
    workflow error shown, which is the whole point of not merging the two.
    """
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    if not can_send_protocol_for_approval(protocol, request.user):
        return render(
            request, 'protocols/detail.html',
            _detail_context(
                request, protocol,
                save_error='Отправить этот протокол на согласование нельзя.',
            ),
            status=403,
        )
    form = ProtocolDraftForm(protocol, request.POST)
    if not form.is_valid():
        return render(
            request, 'protocols/detail.html', _detail_context(request, protocol, form=form),
            status=400,
        )
    try:
        save_protocol_draft(protocol, request.user, form.cleaned)
        send_protocol_for_approval(protocol, request.user)
    except ProtocolWorkflowError as exc:
        # The draft may already be stored — that is intended. The protocol
        # stays in its editable status, so the page re-renders as the editor
        # with the refusal above it.
        protocol.refresh_from_db()
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    protocol.refresh_from_db()
    if protocol.status == Protocol.Status.ARCHIVED:
        # Nobody had to sign, so the same transaction archived it: say so
        # instead of sending the author to an empty «0 из 0» panel.
        messages.success(request, 'Протокол не требовал согласования и помещён в архив.')
    else:
        messages.success(
            request,
            f'Протокол отправлен на согласование (редакция {protocol.revision}).',
        )
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_approve(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        approve_protocol(protocol, request.user)
    except ProtocolWorkflowError as exc:
        protocol.refresh_from_db()
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Протокол согласован.')
    return redirect('protocols:detail', pk=protocol.pk)


@login_required
def protocol_return_for_revision(request, pk):
    if request.method != 'POST':
        return redirect('protocols:detail', pk=pk)
    protocol = get_object_or_404(get_readable_protocols_queryset(), pk=pk)
    try:
        return_protocol_for_revision(protocol, request.user, request.POST.get('comment', ''))
    except ProtocolWorkflowError as exc:
        protocol.refresh_from_db()
        return render(
            request, 'protocols/detail.html',
            _detail_context(request, protocol, save_error=str(exc)), status=400,
        )
    messages.success(request, 'Протокол возвращён на доработку.')
    return redirect('protocols:detail', pk=protocol.pk)


def _detail_context(request, protocol, form=None, save_error=''):
    can_edit = can_edit_protocol(protocol, request.user)
    tab = request.GET.get('tab') or request.POST.get('tab') or 'protocol'
    context = {
        'active_page': 'protocols',
        'header_title': f'{protocol.protocol_type.name} №{protocol.number}',
        'protocol': protocol,
        'detail_tab': 'history' if tab == 'history' else 'protocol',
        'can_edit': can_edit,
        'can_delete': can_delete_draft_protocol(protocol, request.user),
        'save_error': save_error,
        'history_groups': get_protocol_history_groups(protocol),
        'author_participant': protocol.participants.filter(user_id=protocol.author_id).first(),
        # Approval read side. `can_send`/`can_decide` only decide what is
        # rendered; every one of the three endpoints re-checks its own rule.
        'can_send_for_approval': can_send_protocol_for_approval(protocol, request.user),
        'can_decide_approval': can_decide_protocol_approval(protocol, request.user),
        'approval_progress': get_approval_progress(protocol),
        'current_approvals': get_current_approval_rows(protocol),
        'approval_revisions': get_approval_revision_groups(protocol),
        'user_approval': get_user_approval(protocol, request.user),
        'is_under_approval': protocol.status == Protocol.Status.APPROVAL,
    }
    if can_edit:
        directory = get_editor_directory()
        context['form'] = form or ProtocolDraftForm(protocol)
        context.update(directory)
        # The speaker selector offers exactly the current participants; the
        # browser keeps it in step as rows are added or removed.
        context['speaker_options'] = _speaker_options(
            protocol, context['form'], directory['employees']
        )
        # The prototypes the `<template>` elements render; the browser clones
        # them and renumbers, so no markup is assembled in JavaScript.
        context['empty_row'] = {
            'index': 0, 'text': '', 'user': '', 'department': '',
            'speaker': '', 'requires_approval': False, 'errors': {},
        }
        context['empty_action_row'] = {
            'index': 0, 'text': '', 'department': '', 'due_date': '',
            'assignees': [], 'errors': {},
        }
        context['empty_assignee'] = {'user': '', 'department': ''}
    else:
        context['participants'] = protocol.participants.all()
        context['agenda_items'] = protocol.agenda_items.all()
        context['speeches'] = protocol.speeches.select_related('speaker')
        context['actions'] = protocol.actions.select_related('department').prefetch_related(
            'assignees__user'
        )
    return context


def _speaker_options(protocol, form, employees):
    """Author first, then the participant rows as they are currently rendered."""
    names = {
        employee.pk: employee.get_full_name() or employee.get_username()
        for employee in employees
    }
    # A stored participant keeps its snapshot name even if the profile moved on.
    names.update(
        {
            participant.user_id: participant.display_name
            for participant in protocol.participants.all()
        }
    )
    options = [{'value': str(protocol.author_id), 'label': names.get(protocol.author_id, '')}]
    for row in form.participant_rows:
        value = row.get('user') or ''
        user_id = _as_int(value)
        if user_id is None or user_id not in names:
            continue
        options.append({'value': value, 'label': names[user_id]})
    return options


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
