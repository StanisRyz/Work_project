"""«Приём заявок»: the pages of requests filed to a board.

Two audiences, two places. «Заявки» (`/work/requests/`) is for anybody — an
active employee files a request to a board that takes them, a member of it
or not, and follows it in «Мои заявки»; a request's own page
(`boards:request_detail`) is its author's and the board's readers', a 404
for anybody else. «Входящие» (`/work/boards/<board>/inbox/`, in the boards'
frame) and «Приём заявок» (`…/intake/`) are the board's: whoever works on it
sorts the requests, its manager sets the intake up.

Every right is asked before the HTTP method, every write is a POST that
calls `boards/services.py`, a GET on a writing route changes nothing, and a
refusal comes back as the page with what was typed and the message.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from accounts.templatetags.people import person_name

from .forms import AcceptRequestForm, DuplicateRequestForm, IntakeForm, RequestForm
from .models import Board, BoardColumn, BoardField, BoardIntakeHandler, BoardRequest
from .permissions import (
    can_decide_request,
    can_manage_board,
    can_read_inbox,
    can_submit_request,
    can_view_request,
    can_withdraw_request,
    can_work_on_board,
)
from .selectors import build_inbox, build_my_requests, build_request_detail, intake_boards
from .services import (
    BoardError,
    FieldValueError,
    accept_request,
    intake_target,
    mark_duplicate,
    reject_request,
    request_default_due,
    request_form_fields,
    submit_request,
    update_intake,
    withdraw_request,
)
from .views import _board_or_404, _card_url, _frame, _require


def _board_columns(board):
    """Every column of the board with its sub-board, in the board's order — one query."""
    return list(
        BoardColumn.objects.filter(sub_board__board=board).select_related('sub_board')
        .order_by('sub_board__position', 'sub_board_id', 'position', 'pk')
    )


def _request_or_404(request_pk, board=None):
    queryset = BoardRequest.objects.select_related(
        'board', 'author__userprofile__department', 'decided_by', 'card__board', 'duplicate_of__board',
    )
    if board is not None:
        return get_object_or_404(queryset, pk=request_pk, board=board)
    return get_object_or_404(queryset, pk=request_pk)


# --------------------------------------------------------------------------
# «Заявки»: for everybody
# --------------------------------------------------------------------------


@login_required
def requests_home(request):
    """«Подать заявку» (the live boards that take requests) and «Мои заявки»
    (every request of this user, with what became of it). Open to every
    signed-in employee; a fixed number of queries."""
    return render(request, 'boards/requests/home.html', {
        'active_page': 'requests',
        'header_title': 'Заявки',
        'intake_boards': intake_boards(),
        'request_rows': build_my_requests(request.user),
    })


def _submit_context(board, form, error=''):
    return {
        'active_page': 'requests',
        'header_title': f'Заявка на доску «{board.name}»',
        'board': board,
        'form': form,
        'form_error': error,
    }


@login_required
def request_create(request, pk):
    """The request form of one board: its name, its hint and the fields of
    its form — nothing else of the board. A board that does not take
    requests (or is archived) is a 404, the same as one that does not exist."""
    board = get_object_or_404(Board, pk=pk)
    if not can_submit_request(request.user, board):
        raise Http404('Доска не принимает заявки.')
    fields = request_form_fields(board)
    if request.method != 'POST':
        return render(request, 'boards/requests/new.html', _submit_context(board, RequestForm(fields=fields)))
    form = RequestForm(request.POST, fields=fields)
    if form.is_valid():
        try:
            board_request = submit_request(
                board, author=request.user, title=form.cleaned_data['title'],
                description=form.cleaned_data['description'],
                desired_date=form.cleaned_data['desired_date'],
                field_values=form.field_values(),
            )
        except FieldValueError as exc:
            form.add_error(f'field_{exc.field_id}', str(exc))
        except BoardError as exc:
            return render(request, 'boards/requests/new.html', _submit_context(board, form, str(exc)), status=400)
        else:
            messages.success(
                request, f'{board_request.label} подана на доску «{board.name}». Ответ придёт в колокольчик и на почту.',
            )
            return redirect('boards:request_detail', request_pk=board_request.pk)
    return render(request, 'boards/requests/new.html', _submit_context(board, form), status=400)


@login_required
def request_detail(request, request_pk):
    """One request: its author and the board's readers. Whoever sorts the
    board's requests is taken to it in «Входящие», where it is decided;
    anybody else reads it here — the author without a link into a board
    they may not read. Anybody else at all: a 404."""
    board_request = _request_or_404(request_pk)
    if not can_view_request(request.user, board_request):
        raise Http404('Заявка не найдена.')
    if can_read_inbox(request.user, board_request.board):
        return redirect('boards:inbox_request', pk=board_request.board_id, request_pk=board_request.pk)
    return render(request, 'boards/requests/detail.html', {
        'active_page': 'requests',
        'header_title': board_request.label,
        **build_request_detail(board_request, request.user),
        'can_withdraw': can_withdraw_request(request.user, board_request),
    })


@login_required
def request_withdraw(request, request_pk):
    """«Отозвать»: the author's, while the request is new."""
    board_request = _request_or_404(request_pk)
    if not can_view_request(request.user, board_request):
        raise Http404('Заявка не найдена.')
    _require(board_request.author_id == request.user.pk)
    if request.method == 'POST':
        try:
            withdraw_request(board_request, actor=request.user)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'{board_request.label} отозвана.')
            return redirect('boards:requests')
    return redirect('boards:request_detail', request_pk=board_request.pk)


# --------------------------------------------------------------------------
# «Приём заявок»: the board's manager
# --------------------------------------------------------------------------


def _intake_context(request, board, form):
    return {
        **_frame(request, board),
        'header_title': f'Приём заявок · {board.name}',
        'board': board,
        'form': form,
        'requests_url': reverse('boards:request_create', args=[board.pk]),
    }


@login_required
def intake_settings(request, pk):
    """«Приём заявок» of a board — `update_intake()`; the manager's right,
    asked before the method."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    columns = _board_columns(board)
    fields = list(BoardField.objects.filter(board=board, is_archived=False).order_by('position', 'pk'))
    if request.method != 'POST':
        handler_ids = BoardIntakeHandler.objects.filter(board=board).values_list('user_id', flat=True)
        form = IntakeForm(
            board=board, columns=columns, fields=fields,
            initial=IntakeForm.initial_for(board, handler_ids, fields),
        )
        return render(request, 'boards/intake.html', _intake_context(request, board, form))
    form = IntakeForm(request.POST, board=board, columns=columns, fields=fields)
    if form.is_valid():
        try:
            changed = update_intake(
                board, actor=request.user,
                enabled=form.cleaned_data['enabled'],
                column=form.cleaned_data['column'] or None,
                due_days=form.cleaned_data['due_days'],
                hint=form.cleaned_data['hint'],
                handler_ids=[user.pk for user in form.cleaned_data['handlers']],
                form_fields=form.form_fields(),
            )
        except BoardError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, 'Настройки приёма заявок сохранены.' if changed else 'Ничего не изменилось.')
            return redirect('boards:intake', pk=board.pk)
    return render(request, 'boards/intake.html', _intake_context(request, board, form), status=400)


# --------------------------------------------------------------------------
# «Входящие»: whoever works on the board
# --------------------------------------------------------------------------


@login_required
def inbox(request, pk):
    """The board's new requests (oldest first) and those decided in the last
    30 days. Whoever works on the board and full access (`can_read_inbox()`)."""
    board = _board_or_404(pk)
    _require(can_read_inbox(request.user, board))
    return render(request, 'boards/inbox.html', {
        **_frame(request, board),
        'header_title': f'Входящие · {board.name}',
        'board': board,
        **build_inbox(board, request.user),
        'can_manage': can_manage_board(request.user, board),
    })


def _accept_initial(board, board_request):
    """«Принять» starts from the board's intake settings and the request."""
    from .forms import active_members

    _sub_board, column = intake_target(board)
    handler_ids = list(BoardIntakeHandler.objects.filter(board=board).values_list('user_id', flat=True))
    members = set(active_members(board).values_list('pk', flat=True))
    return {
        'column': column.pk if column is not None else None,
        'assignees': [user_id for user_id in handler_ids if user_id in members] or (
            [board.owner_id] if board.owner_id in members else []
        ),
        'due_date': request_default_due(board, board_request),
        'title': board_request.title,
        'description': board_request.description,
    }


def _inbox_request_page(request, board, board_request, *, accept_form=None, duplicate_form=None,
                        error='', status=200):
    can_work = can_work_on_board(request.user, board)
    can_decide = can_decide_request(request.user, board, can_work=can_work) and board_request.is_new
    columns = _board_columns(board) if can_decide else []
    if can_decide and accept_form is None:
        accept_form = AcceptRequestForm(
            board=board, columns=columns, initial=_accept_initial(board, board_request),
        )
    if can_decide and duplicate_form is None:
        duplicate_form = DuplicateRequestForm()
    return render(request, 'boards/inbox_request.html', {
        **_frame(request, board),
        'header_title': f'{board_request.label} · {board.name}',
        'board': board,
        **build_request_detail(board_request, request.user),
        'can_decide': can_decide,
        'accept_form': accept_form,
        'duplicate_form': duplicate_form,
        'decision_error': error,
        'author_name': person_name(board_request.author),
    }, status=status)


@login_required
def inbox_request(request, pk, request_pk):
    """One request in «Входящие»: what it asks, and — while it is new — the
    three decisions."""
    board = _board_or_404(pk)
    _require(can_read_inbox(request.user, board))
    board_request = _request_or_404(request_pk, board)
    return _inbox_request_page(request, board, board_request)


def _deciding(request, pk, request_pk):
    board = _board_or_404(pk)
    _require(can_decide_request(request.user, board))
    return board, _request_or_404(request_pk, board)


@login_required
def request_accept(request, pk, request_pk):
    """«Принять» → `accept_request()`; success opens the new card."""
    board, board_request = _deciding(request, pk, request_pk)
    if request.method != 'POST':
        return redirect('boards:inbox_request', pk=board.pk, request_pk=board_request.pk)
    form = AcceptRequestForm(request.POST, board=board, columns=_board_columns(board))
    if form.is_valid():
        try:
            card = accept_request(
                board_request, actor=request.user,
                column=form.cleaned_data['column'],
                assignee_ids=[user.pk for user in form.cleaned_data['assignees']],
                due_date=form.cleaned_data['due_date'],
                title=form.cleaned_data['title'],
                description=form.cleaned_data['description'],
            )
        except BoardError as exc:
            return _inbox_request_page(request, board, board_request, accept_form=form, error=str(exc), status=400)
        messages.success(request, f'{board_request.label} принята: карточка {card.code}.')
        card.board = board
        return redirect(_card_url(board, card, tab='chat'))
    return _inbox_request_page(request, board, board_request, accept_form=form, status=400)


@login_required
def request_reject(request, pk, request_pk):
    """«Отклонить» — the shared modal posts the reason as `comment`."""
    board, board_request = _deciding(request, pk, request_pk)
    if request.method != 'POST':
        return redirect('boards:inbox_request', pk=board.pk, request_pk=board_request.pk)
    try:
        reject_request(board_request, actor=request.user, reason=request.POST.get('comment', ''))
    except BoardError as exc:
        return _inbox_request_page(request, board, board_request, error=str(exc), status=400)
    messages.success(request, f'{board_request.label} отклонена.')
    return redirect('boards:inbox', pk=board.pk)


@login_required
def request_duplicate(request, pk, request_pk):
    """«Дубль» — the code of a card of this board."""
    board, board_request = _deciding(request, pk, request_pk)
    if request.method != 'POST':
        return redirect('boards:inbox_request', pk=board.pk, request_pk=board_request.pk)
    form = DuplicateRequestForm(request.POST)
    if form.is_valid():
        try:
            board_request = mark_duplicate(
                board_request, actor=request.user,
                card_code=form.cleaned_data['card_code'], comment=form.cleaned_data['comment'],
            )
        except BoardError as exc:
            form.add_error('card_code', str(exc))
        else:
            messages.success(
                request, f'{board_request.label} отмечена дублем карточки {board_request.duplicate_of.code}.',
            )
            return redirect('boards:inbox', pk=board.pk)
    return _inbox_request_page(request, board, board_request, duplicate_form=form, status=400)
