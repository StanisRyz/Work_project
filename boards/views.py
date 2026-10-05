"""The board pages. They work without JavaScript: forms and links only.

Views parse the request, ask `boards.permissions`, call `boards.services` and
render; they decide nothing. The right is asked *before* the HTTP method, so a
typed-in URL without it is a 403 rather than a 405, and every mutating route
answers a GET by going back to the board and changing nothing.

The card panel is part of the board page, chosen by the query string:
`?card=<pk>` reads a card, `&edit=1` edits it, `?new=<stage>` creates one in
that column. A refused or invalid POST renders the board again with the panel
open, the typed values in place and the error beside the form; a successful
one redirects to the board with the card open.

Two blocks of the page are live: the columns and the panel. `boards:fragment`
renders both through the very same context builder and the same partials as
the page, so a refreshed block cannot disagree with a reload.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from realtime.auth import realtime_login_required
from realtime.fragments import content_revision
from tasks.drafts import take_execution_draft
from tasks.forms import TaskAttachmentForm

from .forms import AddMembersForm, BoardForm, CardForm, MoveCardForm
from .models import Board, BoardCard, BoardMember
from .permissions import (
    can_create_board,
    can_manage_board,
    can_view_board,
    can_work_on_board,
)
from .selectors import (
    build_board_list_state,
    build_board_state,
    column_counts,
    resolve_new_stage,
)
from .services import (
    BoardError,
    add_board_members,
    complete_card,
    create_board,
    create_card,
    move_card,
    remove_board_member,
    update_card,
)


def _board_or_404(pk):
    return get_object_or_404(Board.objects.select_related('department', 'owner'), pk=pk)


def _card_url(board, card):
    return f"{reverse('boards:detail', args=[board.pk])}?card={card.pk}"


def _require(allowed):
    if not allowed:
        raise PermissionDenied('Недостаточно прав для работы с доской.')


# --------------------------------------------------------------------------
# Registry and creation
# --------------------------------------------------------------------------


@login_required
def board_list(request):
    state = build_board_list_state(request.user, request.GET.get('tab'))
    state.update({
        'active_page': 'boards',
        'header_title': 'Доски',
        'can_create': can_create_board(request.user),
    })
    return render(request, 'boards/list.html', state)


@login_required
def board_create(request):
    _require(can_create_board(request.user))
    if request.method == 'POST':
        form = BoardForm(request.POST, owner=request.user)
        if form.is_valid():
            try:
                board = create_board(
                    name=form.cleaned_data['name'],
                    description=form.cleaned_data['description'],
                    department=form.cleaned_data['department'],
                    owner=request.user,
                    actor=request.user,
                    member_ids=[user.pk for user in form.cleaned_data['members']],
                )
            except BoardError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, 'Доска создана.')
                return redirect('boards:detail', pk=board.pk)
    else:
        form = BoardForm(owner=request.user)
    return render(request, 'boards/create.html', {
        'active_page': 'boards',
        'header_title': 'Новая доска',
        'form': form,
    })


# --------------------------------------------------------------------------
# The board and its card panel
# --------------------------------------------------------------------------


def _card_initial(item):
    return {
        'title': item['card'].title,
        'description': item['card'].description,
        'due_date': item['due_date'],
        'assignees': [user.pk for user in item['assignees']],
    }


COLUMNS_TEMPLATE = 'boards/includes/columns.html'
PANEL_TEMPLATE = 'boards/includes/panel.html'


def _board_context(request, board, *, card_id=None, edit=False, new=None, panel=None,
                   form=None, move_form=None, error='', execution_comment=None,
                   execution_error='', take_draft=True):
    """Everything the board page and its live fragment render.

    `panel` is `'view'`, `'edit'` or `'new'`; `None` decides it from `card_id`,
    `edit` and `new` — the query string's `card`, `edit=1` and `new`. A form
    passed in is shown as it is — bound, with its errors — so a refused POST
    keeps what was typed.

    «Выполнение» starts from what was just posted (a refused completion), else
    from the draft an upload or a deletion parked in the session, else from the
    task's own result — so a task an administrator reopened shows what was
    written before, exactly as the task page does. The live fragment passes
    `take_draft=False`: the draft is the page's to show, once.

    Returns the context and whether the panel holds input that is not the
    stored state (a bound form, posted or parked text): such a page starts
    «dirty» for the live client, and its panel fingerprint comes from a clean
    render.
    """
    state = build_board_state(board, request.user, card_id=card_id)
    item = state['card']
    can_edit_card = bool(item and state['can_work'] and not item['is_closed'])
    new_stage = None
    holds_input = bool(
        (form is not None and form.is_bound)
        or (move_form is not None and move_form.is_bound)
        or execution_comment is not None
    )
    if panel is None:
        new_stage = resolve_new_stage(new) if state['can_work'] else None
        if item is not None:
            panel = 'edit' if edit and can_edit_card else 'view'
        elif new_stage is not None:
            panel = 'new'
    elif panel == 'new':
        submitted = form.data.get('stage') if form is not None else ''
        new_stage = resolve_new_stage(submitted) or resolve_new_stage(BoardCard.Stage.TODO)
    if panel == 'edit' and form is None:
        form = CardForm(board=board, initial=_card_initial(item))
    if panel == 'new' and form is None:
        form = CardForm(board=board, initial={'stage': new_stage.code})
    if panel == 'view' and can_edit_card and move_form is None:
        move_form = MoveCardForm(initial={'stage': item['card'].stage})
    if item is not None and panel == 'view' and item['can_complete'] and execution_comment is None:
        draft = take_execution_draft(request, item['task']) if take_draft else None
        holds_input = holds_input or bool(draft)
        execution_comment = draft or item['task'].execution_comment
    board_url = reverse('boards:detail', args=[board.pk])
    state.update({
        'active_page': 'boards',
        'header_title': board.name,
        'panel': panel,
        'form': form,
        'move_form': move_form,
        'new_stage': new_stage,
        'can_edit_card': can_edit_card,
        'panel_error': error,
        # The task's own names, so the shared «Вложения» include reads the
        # same context here as on the task page.
        'task': item['task'] if item else None,
        'attachments': item['attachments'] if item else [],
        'can_upload_attachment': bool(item and item['can_upload_attachment']),
        'attachment_form': TaskAttachmentForm(),
        'list_query': '',
        'execution_comment': execution_comment or '',
        'execution_error': execution_error,
        'board_url': board_url,
    })
    query = _panel_query(item, panel, new_stage)
    state['fragment_url'] = reverse('boards:fragment', args=[board.pk]) + query
    state['page_url'] = board_url + query
    return state, holds_input


def _panel_query(item, panel, new_stage):
    """The query string that asks for exactly the panel this page shows.

    It goes on the live fragment's URL and on the page's own address for a
    reload. Built by the server, not read off the address bar: a board drawn
    in answer to a refused POST stands at the POST's URL, whose query string
    says nothing about the panel — and reloading that URL would post again.
    """
    query = {}
    if panel in ('view', 'edit') and item is not None:
        query['card'] = item['card'].pk
        if panel == 'edit':
            query['edit'] = '1'
    elif panel == 'new' and new_stage is not None:
        query['new'] = new_stage.code
    return f'?{urlencode(query)}' if query else ''


def _board_blocks(request, context):
    """The two live blocks as markup, each with its fingerprint."""
    columns_html = render_to_string(COLUMNS_TEMPLATE, context, request=request)
    panel_html = (
        render_to_string(PANEL_TEMPLATE, context, request=request) if context['panel'] else ''
    )
    return {
        'columns_html': columns_html,
        'columns_revision': content_revision(columns_html),
        'panel_html': panel_html,
        'panel_revision': content_revision(panel_html) if panel_html else '',
    }


def _render_board(request, board, *, status=200, **options):
    """The board page with its panel; the one renderer every board view uses.

    The columns and the panel are rendered once, by `_board_blocks()`, and the
    page prints that markup next to its fingerprint — the same pair the live
    fragment returns. A panel holding a bound form or unsaved text takes its
    fingerprint from a clean render instead (what the fragment would return),
    so the live client compares like with like, and the page tells the client
    it starts with unsaved input.
    """
    context, holds_input = _board_context(request, board, **options)
    blocks = _board_blocks(request, context)
    if holds_input and context['panel']:
        clean, _ = _board_context(
            request, board,
            card_id=context['card']['card'].pk if context['card'] else None,
            edit=context['panel'] == 'edit',
            new=context['new_stage'].code if context['new_stage'] else None,
            take_draft=False,
        )
        blocks['panel_revision'] = _board_blocks(request, clean)['panel_revision']
    context.update(blocks)
    context['panel_holds_input'] = holds_input
    return render(request, 'boards/detail.html', context, status=status)


@login_required
def board_detail(request, pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    return _render_board(
        request, board,
        card_id=request.GET.get('card'),
        edit=request.GET.get('edit') == '1',
        new=request.GET.get('new'),
    )


@realtime_login_required
@require_GET
def board_fragment(request, pk):
    """The columns and the panel of one board, for the live client.

    The same right as the page, the same query string (`card`, `edit`, `new`),
    the same context builder and the same partials: a refreshed block is what
    a reload would draw. JSON, never cached, nothing written — the session
    draft of «Выполнение» stays where it is.
    """
    board = _board_or_404(pk)
    if not can_view_board(request.user, board):
        return _no_cache(JsonResponse({'error': 'forbidden'}, status=403))
    context, _ = _board_context(
        request, board,
        card_id=request.GET.get('card'),
        edit=request.GET.get('edit') == '1',
        new=request.GET.get('new'),
        take_draft=False,
    )
    return _no_cache(JsonResponse({
        **_board_blocks(request, context),
        'panel': context['panel'] or '',
        'generated_at': timezone.now().isoformat(),
    }))


def _no_cache(response):
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


@login_required
def card_create(request, pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    if request.method != 'POST':
        return redirect('boards:detail', pk=board.pk)
    form = CardForm(request.POST, board=board)
    if form.is_valid():
        try:
            card = create_card(
                board,
                actor=request.user,
                title=form.cleaned_data['title'],
                description=form.cleaned_data['description'],
                due_date=form.cleaned_data['due_date'],
                assignee_ids=[user.pk for user in form.cleaned_data['assignees']],
                stage=form.cleaned_data['stage'] or BoardCard.Stage.TODO,
            )
        except BoardError as exc:
            return _render_board(request, board, panel='new', form=form, error=str(exc))
        return redirect(_card_url(board, card))
    return _render_board(request, board, panel='new', form=form)


@login_required
def card_update(request, pk, card_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card))
    form = CardForm(request.POST, board=board)
    if form.is_valid():
        try:
            update_card(
                card,
                actor=request.user,
                title=form.cleaned_data['title'],
                description=form.cleaned_data['description'],
                due_date=form.cleaned_data['due_date'],
                assignee_ids=[user.pk for user in form.cleaned_data['assignees']],
            )
        except BoardError as exc:
            return _render_board(
                request, board, card_id=card.pk, panel='edit', form=form, error=str(exc),
            )
        return redirect(_card_url(board, card))
    return _render_board(request, board, card_id=card.pk, panel='edit', form=form)


# A request from `static/js/board_dnd.js`: it asks for JSON by this header and
# nothing else — `Accept` would not do, a browser's ordinary form POST already
# accepts `*/*`.
FETCH_HEADER_VALUE = 'fetch'


def _is_fetch(request):
    return request.headers.get('X-Requested-With') == FETCH_HEADER_VALUE


@login_required
def card_move(request, pk, card_pk):
    """Put a card in a working column: «Переместить в…», or a drag on the board.

    An ordinary POST behaves as it always has — redirect to the card, or the
    board re-rendered with the refusal. A drag (`X-Requested-With: fetch`) gets
    JSON instead: `{"ok": true, "stage", "counts"}` on success, `400
    {"ok": false, "error"}` when the form or `move_card()` refuses, `403` when
    the right is missing — still asked before the method. The answer carries
    identifiers, column codes and counts only: no markup, no card text, no
    rights. The browser decides nothing; it only undoes its optimistic move.
    """
    board = _board_or_404(pk)
    fetch = _is_fetch(request)
    if not can_work_on_board(request.user, board):
        if fetch:
            return JsonResponse(
                {'ok': False, 'error': 'Работа с карточками этой доски недоступна.'}, status=403,
            )
        _require(False)
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card))
    form = MoveCardForm(request.POST)
    if not form.is_valid():
        if fetch:
            return JsonResponse({'ok': False, 'error': 'Неверная колонка или позиция.'}, status=400)
        return _render_board(
            request, board, card_id=card.pk, panel='view', move_form=form,
            error='Выберите колонку.',
        )
    try:
        card = move_card(
            card,
            actor=request.user,
            stage=form.cleaned_data['stage'],
            before_card_id=form.cleaned_data['before_card_id'],
        )
    except BoardError as exc:
        if fetch:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        return _render_board(
            request, board, card_id=card.pk, panel='view', move_form=form, error=str(exc),
        )
    if fetch:
        return JsonResponse({'ok': True, 'stage': card.stage, 'counts': column_counts(board)})
    return redirect(_card_url(board, card))


@login_required
def card_complete(request, pk, card_pk):
    """«Завершить» in the card panel: `complete_card()`, i.e. `complete_task()`.

    Reading the board is what is asked before the method; who may finish the
    work is the task's own rule (`can_complete_task()`), answered by
    `complete_task()` under the task's lock — the board adds none. A refusal
    (an empty result, somebody who is not an исполнитель, a task already
    closed) comes back as the board with the panel open, the text in the field
    and the message beside it.
    """
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card))
    execution_comment = request.POST.get('execution_comment', '')
    try:
        complete_card(card, actor=request.user, execution_comment=execution_comment)
    except BoardError as exc:
        return _render_board(
            request, board, card_id=card.pk, panel='view',
            execution_comment=execution_comment, execution_error=str(exc),
        )
    messages.success(request, 'Задача выполнена, карточка в колонке «Готово».')
    return redirect(_card_url(board, card))


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------


@login_required
def board_members(request, pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    can_manage = can_manage_board(request.user, board)
    members = (
        BoardMember.objects.filter(board=board)
        .select_related('user__userprofile__department')
        .order_by('user__last_name', 'user__first_name', 'user__username')
    )
    return render(request, 'boards/members.html', {
        'active_page': 'boards',
        'header_title': f'Участники · {board.name}',
        'board': board,
        'members': members,
        'can_manage': can_manage,
        'add_form': AddMembersForm(board=board) if can_manage else None,
    })


@login_required
def members_add(request, pk):
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method != 'POST':
        return redirect('boards:members', pk=board.pk)
    form = AddMembersForm(request.POST, board=board)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect('boards:members', pk=board.pk)
    try:
        added = add_board_members(
            board, [user.pk for user in form.cleaned_data['users']], actor=request.user,
        )
    except BoardError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Добавлено участников: {len(added)}.')
    return redirect('boards:members', pk=board.pk)


@login_required
def member_remove(request, pk, user_pk):
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method != 'POST':
        return redirect('boards:members', pk=board.pk)
    user = get_object_or_404(get_user_model(), pk=user_pk)
    try:
        remove_board_member(board, user, actor=request.user)
    except BoardError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Участник исключён.')
    return redirect('boards:members', pk=board.pk)
