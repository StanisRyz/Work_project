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
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .forms import AddMembersForm, BoardForm, CardForm, MoveCardForm
from .models import Board, BoardCard, BoardMember
from .permissions import (
    can_create_board,
    can_manage_board,
    can_view_board,
    can_work_on_board,
)
from .selectors import build_board_list_state, build_board_state, resolve_new_stage
from .services import (
    BoardError,
    add_board_members,
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


def _render_board(request, board, *, card_id=None, panel=None, form=None, move_form=None,
                  error='', status=200):
    """The board page with its panel; the one renderer every board view uses.

    `panel` is `'view'`, `'edit'` or `'new'`; `None` decides it from the query
    string. A form passed in is shown as it is — bound, with its errors — so a
    refused POST keeps what was typed.
    """
    state = build_board_state(board, request.user, card_id=card_id)
    item = state['card']
    can_edit_card = bool(item and state['can_work'] and not item['is_closed'])
    new_stage = None
    if panel is None:
        new_stage = resolve_new_stage(request.GET.get('new')) if state['can_work'] else None
        if item is not None:
            panel = 'edit' if request.GET.get('edit') == '1' and can_edit_card else 'view'
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
    state.update({
        'active_page': 'boards',
        'header_title': board.name,
        'panel': panel,
        'form': form,
        'move_form': move_form,
        'new_stage': new_stage,
        'can_edit_card': can_edit_card,
        'panel_error': error,
        'board_url': reverse('boards:detail', args=[board.pk]),
    })
    return render(request, 'boards/detail.html', state, status=status)


@login_required
def board_detail(request, pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    return _render_board(request, board, card_id=request.GET.get('card'))


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


@login_required
def card_move(request, pk, card_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card))
    form = MoveCardForm(request.POST)
    if form.is_valid():
        try:
            move_card(card, actor=request.user, stage=form.cleaned_data['stage'])
        except BoardError as exc:
            return _render_board(
                request, board, card_id=card.pk, panel='view', move_form=form, error=str(exc),
            )
        return redirect(_card_url(board, card))
    return _render_board(
        request, board, card_id=card.pk, panel='view', move_form=form,
        error='Выберите колонку.',
    )


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
