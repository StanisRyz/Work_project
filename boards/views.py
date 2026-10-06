"""The board pages. They work without JavaScript: forms and links only.

Every page is drawn in one frame (`boards/layout.html`): the boards this user
reads on the left (`_frame()`), the page on the right. There is no registry —
`/work/boards/` goes to the sub-board opened last, else to the first board.

Views parse the request, ask `boards.permissions`, call `boards.services` and
render; they decide nothing. The right is asked *before* the HTTP method, so a
typed-in URL without it is a 403 rather than a 405, and every mutating route
answers a GET by going back to the board and changing nothing.

A board is read one sub-board at a time: `/work/boards/<board>/` sends the
reader to its first tab (or to the tab of the card `?card=` names), and
`/work/boards/<board>/<sub_board>/` is the page. The card panel is part of it,
chosen by the query string: `?card=<pk>` reads a card, `&edit=1` edits it,
`?new=<column id>` creates one in that column. A refused or invalid POST
renders the sub-board again with the panel open, the typed values in place
and the error beside the form; a successful one redirects to the card's
sub-board with the card open.

Four blocks of the page are live: the tabs, the columns, the panel and the
open card's messages. `boards:fragment` renders them through the very
same context builder and the same partials as the page, so a refreshed block
cannot disagree with a reload.

The structure routes (tabs and columns: create, rename, ←/→, delete) are small
POST forms for whoever manages the board; a refusal comes back as a message on
the sub-board.
"""

from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.directory import get_employee_directory
from realtime.auth import realtime_login_required
from realtime.fragments import content_revision
from tasks.drafts import remember_execution_draft, take_execution_draft
from tasks.forms import TaskAttachmentForm

from .forms import (
    AddMembersForm,
    BoardForm,
    BoardNameForm,
    CardForm,
    ColumnNameForm,
    DirectionForm,
    MoveCardForm,
    SubBoardNameForm,
)
from .models import Board, BoardCard, BoardColumn, BoardMember, SubBoard
from .permissions import (
    can_cancel_card,
    can_comment_card,
    can_create_board,
    can_manage_board,
    can_restore_board,
    can_view_board,
    can_work_on_board,
)
from .selectors import (
    build_board_nav,
    build_board_state,
    column_counts,
    first_sub_board,
    member_preview,
    parse_board_filters,
    resolve_new_column,
)
from .services import (
    BoardError,
    StaleCardError,
    add_board_members,
    archive_board,
    cancel_card,
    complete_card,
    create_board,
    create_card,
    create_column,
    create_sub_board,
    delete_column,
    delete_sub_board,
    move_card,
    move_column,
    move_sub_board,
    post_card_comment,
    remove_board_member,
    rename_board,
    rename_column,
    rename_sub_board,
    restore_board,
    update_card,
)


def _board_or_404(pk):
    return get_object_or_404(Board.objects.select_related('department', 'owner'), pk=pk)


def _sub_board_or_404(board, sub_pk):
    return get_object_or_404(SubBoard, pk=sub_pk, board=board)


def _sub_board_url(board, sub_board_id):
    return reverse('boards:sub_board', args=[board.pk, sub_board_id])


def _card_url(board, card, request=None):
    """The card's sub-board with `card` open — and, from a request, the filter
    it was under.

    Every board form posts to a URL carrying the board's filter query, so the
    redirect after it lands on the same filtered sub-board.
    """
    url = f'{_sub_board_url(board, card.sub_board_id)}?card={card.pk}'
    query = parse_board_filters(request.GET).query if request is not None else ''
    return f'{url}&{query}' if query else url


def _require(allowed):
    if not allowed:
        raise PermissionDenied('Недостаточно прав для работы с доской.')


# --------------------------------------------------------------------------
# Registry and creation
# --------------------------------------------------------------------------


# The sub-board this browser session opened last; `/work/boards/` goes back to it.
LAST_SUB_BOARD_SESSION_KEY = 'boards_last_sub_board'


def _frame(request, current_board=None):
    """What `boards/layout.html` draws around every board page: the left panel.

    The same list on every page (`build_board_nav()`, one query), the board
    the page shows highlighted, and «+» only for whoever may create a board.
    """
    return {
        'active_page': 'boards',
        'board_nav': build_board_nav(request.user, current_board),
        'can_create': can_create_board(request.user),
    }


@login_required
def board_list(request):
    """`/work/boards/`: no registry — the last sub-board opened, else the first board.

    The last one is remembered in the session by every page of a sub-board,
    and taken only while it still exists and the user still reads its board.
    Otherwise the first live board of the left panel. With none, the frame
    with an empty state: «Создать доску» for whoever may, otherwise «Вас пока
    не добавили ни на одну доску». Open to every signed-in employee — it shows
    nothing they could not see — while the menu still offers «Доски» only to
    whoever uses boards.
    """
    remembered = request.session.get(LAST_SUB_BOARD_SESSION_KEY)
    if remembered:
        sub_board = (
            SubBoard.objects.select_related('board').filter(pk=remembered).first()
            if str(remembered).isdigit() else None
        )
        if sub_board is not None and can_view_board(request.user, sub_board.board):
            return redirect('boards:sub_board', pk=sub_board.board_id, sub_pk=sub_board.pk)
        request.session.pop(LAST_SUB_BOARD_SESSION_KEY, None)
    frame = _frame(request)
    if frame['board_nav']['boards']:
        return redirect('boards:detail', pk=frame['board_nav']['boards'][0].pk)
    frame['header_title'] = 'Доски'
    return render(request, 'boards/empty.html', frame)


def _member_picker(posted=(), *, extra_row=False, exclude=()):
    """What `boards/includes/member_picker.html` draws: the directory and the rows.

    A row per posted employee (each with its own department chosen beside it,
    so a re-rendered form reads as it was filled), plus one empty row when
    nothing was posted or «+ Добавить участника» asked for one without
    JavaScript. The directory is `accounts.directory.get_employee_directory()`,
    the protocol editor's own.
    """
    directory = get_employee_directory()
    employees = list(directory['employees'])
    departments_of = {
        str(user.pk): str(getattr(getattr(user, 'userprofile', None), 'department_id', '') or '')
        for user in employees
    }
    rows = [
        {'user': value, 'department': departments_of.get(value, '')}
        for value in (str(item).strip() for item in posted) if value
    ]
    if extra_row or not rows:
        rows.append({'user': '', 'department': ''})
    return {
        'departments': directory['departments'],
        'employees': employees,
        'member_rows': rows,
        'empty_member_row': {'user': '', 'department': ''},
        'picker_exclude': ','.join(str(pk) for pk in exclude),
    }


@login_required
def board_create(request):
    _require(can_create_board(request.user))
    posted = ()
    if request.method == 'POST':
        posted = request.POST.getlist('members')
        form = BoardForm(request.POST, owner=request.user)
        if 'add_member_row' in request.POST:
            # «+ Добавить участника» without JavaScript: the same form again
            # with one more row, nothing written and no errors shown.
            form = BoardForm(initial={'name': request.POST.get('name', '')}, owner=request.user)
        elif form.is_valid():
            try:
                board = create_board(
                    name=form.cleaned_data['name'],
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
        **_frame(request),
        'header_title': 'Новая доска',
        'form': form,
        **_member_picker(
            posted, extra_row='add_member_row' in request.POST, exclude=[request.user.pk],
        ),
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
        'version': item['card'].version,
    }


TABS_TEMPLATE = 'boards/includes/tabs.html'
COLUMNS_TEMPLATE = 'boards/includes/columns.html'
PANEL_TEMPLATE = 'boards/includes/panel.html'
COMMENTS_TEMPLATE = 'boards/includes/comments.html'


def _board_context(request, board, sub_board, *, card_id=None, edit=False, new=None, panel=None,
                   form=None, move_form=None, error='', execution_comment=None,
                   execution_error='', take_draft=True, version_conflict=False,
                   comment_text='', comment_error=''):
    """Everything the board page and its live fragment render.

    `panel` is `'view'`, `'edit'` or `'new'`; `None` decides it from `card_id`,
    `edit` and `new` — the query string's `card`, `edit=1` and `new` (a
    working column's id). A form
    passed in is shown as it is — bound, with its errors — so a refused POST
    keeps what was typed.

    «Выполнение» starts from what was just posted (a refused completion), else
    from the draft an upload or a deletion parked in the session, else from the
    task's own result — so a task an administrator reopened shows what was
    written before, exactly as the task page does. The live fragment passes
    `take_draft=False`: the draft is the page's to show, once.

    The filters (`?mine=1`, `?overdue=1`, `?q=`) are read from `request.GET`
    here and nowhere else — on a POST too, whose action URL carries them — so
    the page, its fragment and a refused form are filtered alike, and every
    link the panel and the tiles draw keeps them (`filter_query`).

    Returns the context and whether the panel holds input that is not the
    stored state (a bound form, posted or parked text): such a page starts
    «dirty» for the live client, and its panel fingerprint comes from a clean
    render.
    """
    filters = parse_board_filters(request.GET)
    all_comments = request.GET.get('comments') == 'all'
    state = build_board_state(
        board, sub_board, request.user,
        card_id=card_id, filters=filters, all_comments=all_comments,
    )
    item = state['card']
    columns = [row['column'] for row in state['columns']]
    can_edit_card = bool(item and state['can_work'] and not item['is_closed'])
    new_column = None
    holds_input = bool(
        (form is not None and form.is_bound)
        or (move_form is not None and move_form.is_bound)
        or execution_comment is not None
    )
    if panel is None:
        new_column = resolve_new_column(columns, new) if state['can_work'] else None
        if item is not None:
            panel = 'edit' if edit and can_edit_card else 'view'
        elif new_column is not None:
            panel = 'new'
    elif panel == 'new':
        submitted = form.data.get('column') if form is not None else ''
        new_column = resolve_new_column(columns, submitted) or state['first_working_column']
    if panel == 'edit' and form is None:
        form = CardForm(board=board, initial=_card_initial(item))
    if panel == 'new' and form is None:
        form = CardForm(board=board, initial={'column': new_column.pk})
    if panel == 'view' and can_edit_card and move_form is None:
        move_form = MoveCardForm(
            initial={'column_id': item['column'].pk if item['column'] else None},
        )
    if move_form is not None:
        # The select offers this sub-board's working columns.
        move_form.fields['column_id'].widget.choices = [
            (column.pk, column.name) for column in columns if not column.is_done
        ]
    if item is not None and panel == 'view' and item['can_complete'] and execution_comment is None:
        draft = take_execution_draft(request, item['task']) if take_draft else None
        holds_input = holds_input or bool(draft)
        execution_comment = draft or item['task'].execution_comment
    board_url = _sub_board_url(board, sub_board.pk)
    state.update({
        'active_page': 'boards',
        'header_title': board.name,
        'panel': panel,
        'form': form,
        'move_form': move_form,
        'new_column': new_column,
        'sub_board_name_form': SubBoardNameForm(),
        'column_name_form': ColumnNameForm(),
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
        'filter_query': filters.query,
        'filter_suffix': f'?{filters.query}' if filters.query else '',
        'version_conflict': version_conflict,
        # «Обсуждение»'s form — outside every live block, so a refresh never
        # redraws what is being typed in it.
        'show_discussion': bool(item is not None and panel == 'view'),
        'comment_text': comment_text,
        'comment_error': comment_error,
    })
    query = _panel_query(item, panel, new_column, filters, all_comments=all_comments)
    state['fragment_url'] = reverse('boards:fragment', args=[board.pk, sub_board.pk]) + query
    state['page_url'] = board_url + query
    # «Показать ранние (N)»: this very panel with every message.
    state['all_comments_url'] = (
        board_url + _panel_query(item, panel, new_column, filters, all_comments=True)
        if state['show_discussion'] else ''
    )
    return state, holds_input


def _panel_query(item, panel, new_column, filters, *, all_comments=False):
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
        elif all_comments:
            query['comments'] = 'all'
    elif panel == 'new' and new_column is not None:
        query['new'] = new_column.pk
    encoded = '&'.join(part for part in (urlencode(query), filters.query) if part)
    return f'?{encoded}' if encoded else ''


def _board_blocks(request, context):
    """The four live blocks as markup, each with its fingerprint.

    The tabs and the columns are read-only blocks of their own (the filter row
    stands between them on the page), so a tab or a column created, renamed,
    moved or deleted by somebody else arrives with the cards.

    The messages of «Обсуждение» are a block of their own and appear in no
    other: a new message moves `comments_revision` (and the tile's counter in
    `columns_revision`), never `panel_revision`, so it cannot raise the
    conflict banner over a «Выполнение» or an edit being typed.
    """
    tabs_html = render_to_string(TABS_TEMPLATE, context, request=request)
    columns_html = render_to_string(COLUMNS_TEMPLATE, context, request=request)
    panel_html = (
        render_to_string(PANEL_TEMPLATE, context, request=request) if context['panel'] else ''
    )
    comments_html = (
        render_to_string(COMMENTS_TEMPLATE, context, request=request)
        if context['show_discussion'] else ''
    )
    return {
        'tabs_html': tabs_html,
        'tabs_revision': content_revision(tabs_html),
        'columns_html': columns_html,
        'columns_revision': content_revision(columns_html),
        'panel_html': panel_html,
        'panel_revision': content_revision(panel_html) if panel_html else '',
        'comments_html': comments_html,
        'comments_revision': content_revision(comments_html) if comments_html else '',
    }


def _render_board(request, board, sub_board, *, status=200, **options):
    """The board page with its panel; the one renderer every board view uses.

    The columns and the panel are rendered once, by `_board_blocks()`, and the
    page prints that markup next to its fingerprint — the same pair the live
    fragment returns. A panel holding a bound form or unsaved text takes its
    fingerprint from a clean render instead (what the fragment would return),
    so the live client compares like with like, and the page tells the client
    it starts with unsaved input.
    """
    context, holds_input = _board_context(request, board, sub_board, **options)
    blocks = _board_blocks(request, context)
    if holds_input and context['panel']:
        clean, _ = _board_context(
            request, board, sub_board,
            card_id=context['card']['card'].pk if context['card'] else None,
            edit=context['panel'] == 'edit',
            new=context['new_column'].pk if context['new_column'] else None,
            take_draft=False,
        )
        blocks['panel_revision'] = _board_blocks(request, clean)['panel_revision']
    context.update(blocks)
    context['panel_holds_input'] = holds_input
    # The frame and the heading are the page's only — never part of a
    # fragment, so a live refresh never pays for them.
    context.update(_frame(request, board))
    members = member_preview(board)
    context['member_preview'] = members
    context['member_more'] = max(context['member_count'] - len(members), 0)
    request.session[LAST_SUB_BOARD_SESSION_KEY] = sub_board.pk
    return render(request, 'boards/detail.html', context, status=status)


@login_required
def board_detail(request, pk):
    """`/work/boards/<board>/`: the board's first tab, the query string kept.

    `?card=<pk>` of a card of this board leads to that card's own tab — the
    address every older link and notification still carries.
    """
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    card_id = request.GET.get('card')
    card = None
    if card_id and card_id.isdigit():
        card = BoardCard.objects.filter(pk=int(card_id), board=board).only('sub_board_id').first()
    sub_board_id = card.sub_board_id if card is not None else getattr(first_sub_board(board), 'pk', None)
    if sub_board_id is None:
        raise Http404('У доски нет поддосок.')
    url = _sub_board_url(board, sub_board_id)
    query = request.GET.urlencode()
    return redirect(f'{url}?{query}' if query else url)


@login_required
def sub_board_detail(request, pk, sub_pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    sub_board = _sub_board_or_404(board, sub_pk)
    return _render_board(
        request, board, sub_board,
        card_id=request.GET.get('card'),
        edit=request.GET.get('edit') == '1',
        new=request.GET.get('new'),
    )


@realtime_login_required
@require_GET
def board_fragment(request, pk, sub_pk):
    """The live blocks of one sub-board, for the live client.

    The same right as the page, the same query string (`card`, `edit`, `new`),
    the same context builder and the same partials: a refreshed block is what
    a reload would draw. JSON, never cached, nothing written — the session
    draft of «Выполнение» stays where it is. A sub-board deleted meanwhile is
    a 404, which ends the live client of that page.
    """
    board = _board_or_404(pk)
    if not can_view_board(request.user, board):
        return _no_cache(JsonResponse({'error': 'forbidden'}, status=403))
    sub_board = SubBoard.objects.filter(pk=sub_pk, board=board).first()
    if sub_board is None:
        return _no_cache(JsonResponse({'error': 'not_found'}, status=404))
    context, _ = _board_context(
        request, board, sub_board,
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
def card_create(request, pk, sub_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    sub_board = _sub_board_or_404(board, sub_pk)
    if request.method != 'POST':
        return redirect(_sub_board_url(board, sub_board.pk))
    form = CardForm(request.POST, board=board)
    if form.is_valid():
        try:
            card = create_card(
                sub_board,
                actor=request.user,
                column=form.cleaned_data['column'],
                title=form.cleaned_data['title'],
                description=form.cleaned_data['description'],
                due_date=form.cleaned_data['due_date'],
                assignee_ids=[user.pk for user in form.cleaned_data['assignees']],
            )
        except BoardError as exc:
            return _render_board(request, board, sub_board, panel='new', form=form, error=str(exc))
        return redirect(_card_url(board, card, request))
    return _render_board(request, board, sub_board, panel='new', form=form)


@login_required
def card_update(request, pk, card_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
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
                expected_version=form.cleaned_data['version'],
            )
        except BoardError as exc:
            # A stale version keeps the typed values in the edit panel and
            # offers the current card in another tab, so nothing typed is lost.
            return _render_board(
                request, board, card.sub_board, card_id=card.pk, panel='edit', form=form,
                error=str(exc), version_conflict=isinstance(exc, StaleCardError),
            )
        return redirect(_card_url(board, card, request))
    return _render_board(request, board, card.sub_board, card_id=card.pk, panel='edit', form=form)


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
    sub-board re-rendered with the refusal. A drag (`X-Requested-With: fetch`)
    gets JSON instead: `{"ok": true, "column_id", "counts"}` on success, the
    counts keyed by column id; `400 {"ok": false, "error"}` when the form or
    `move_card()` refuses (the closing column, a column of another sub-board,
    one deleted meanwhile); `403` when the right is missing — still asked
    before the method. Identifiers and counts only: no markup, no card text,
    no rights. The browser decides nothing; it only undoes its optimistic move.
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
        return redirect(_card_url(board, card, request))
    form = MoveCardForm(request.POST)
    if not form.is_valid():
        if fetch:
            return JsonResponse({'ok': False, 'error': 'Неверная колонка или позиция.'}, status=400)
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view', move_form=form,
            error='Выберите колонку.',
        )
    try:
        card = move_card(
            card,
            actor=request.user,
            column=form.cleaned_data['column_id'],
            before_card_id=form.cleaned_data['before_card_id'],
        )
    except BoardError as exc:
        if fetch:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view', move_form=form,
            error=str(exc),
        )
    if fetch:
        # Counted under the filter the board is drawn with: the move URL of a
        # tile carries it, exactly as the board's forms do.
        counts = column_counts(card.sub_board, request.user, parse_board_filters(request.GET))
        return JsonResponse({'ok': True, 'column_id': card.column_id, 'counts': counts})
    return redirect(_card_url(board, card, request))


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
        return redirect(_card_url(board, card, request))
    execution_comment = request.POST.get('execution_comment', '')
    try:
        complete_card(card, actor=request.user, execution_comment=execution_comment)
    except BoardError as exc:
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view',
            execution_comment=execution_comment, execution_error=str(exc),
        )
    messages.success(request, 'Задача выполнена, карточка в завершающей колонке.')
    return redirect(_card_url(board, card, request))


@login_required
def card_cancel(request, pk, card_pk):
    """«Отменить карточку» in the panel, confirmed with a required reason.

    The right (`can_cancel_card()`: author, owner, administrator — never an
    archived board) is asked before the method; `cancel_card()` asks it again
    under the locks. The reason arrives as `cancellation_reason`, the name the
    panel's confirmation trigger gives the modal's comment.
    """
    board = _board_or_404(pk)
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    _require(can_cancel_card(request.user, card))
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
    try:
        cancel_card(card, actor=request.user, reason=request.POST.get('cancellation_reason', ''))
    except BoardError as exc:
        return _render_board(request, board, card.sub_board, card_id=card.pk, panel='view', error=str(exc))
    messages.success(request, 'Карточка отменена: её задача закрыта без выполнения.')
    return redirect(_card_url(board, card, request))


@login_required
def card_comment(request, pk, card_pk):
    """«Отправить» in the card's «Обсуждение»: `post_card_comment()`.

    The right (`can_comment_card()`) is asked before the method. Success goes
    back to the card under the board's filter; a refusal re-renders the panel
    with the text and the message beside the form.
    """
    board = _board_or_404(pk)
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    _require(can_comment_card(request.user, card))
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
    text = request.POST.get('text', '')
    _remember_draft(request, card)
    try:
        post_card_comment(card, actor=request.user, text=text)
    except BoardError as exc:
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view',
            comment_text=text, comment_error=str(exc),
        )
    return redirect(_card_url(board, card, request))


def _remember_draft(request, card):
    """Park the «Выполнение» text the message form carried, as an upload does.

    The form's hidden `execution_comment` is filled from the panel's textarea
    at submit time (`[data-attachment-carry-from]`), and the panel drawn next —
    after the redirect, or the refusal re-rendered here — takes it back. Only
    a form that carried the field speaks for the draft: one without it (no
    «Выполнение» on the panel) leaves the session alone. A draft, never a
    result — `complete_task()` is still the only writer of
    `Task.execution_comment`.
    """
    if 'execution_comment' not in request.POST:
        return
    task = card.tasks.first()  # the one `BOARD` task — `unique_board_card_task`
    if task is not None:
        remember_execution_draft(request, task, request.POST['execution_comment'])


@login_required
def board_rename(request, pk):
    """«Переименовать» in the board's «⋯» menu: `rename_board()`."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method == 'POST':
        form = BoardNameForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                rename_board(board, actor=request.user, name=form.cleaned_data['name'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back_to_board(request, board)


def _back_to_board(request, board):
    """The sub-board the form was posted from (`?sub=`), else the board."""
    sub_pk = request.GET.get('sub')
    if sub_pk and sub_pk.isdigit() and SubBoard.objects.filter(pk=sub_pk, board=board).exists():
        return redirect('boards:sub_board', pk=board.pk, sub_pk=int(sub_pk))
    return redirect('boards:detail', pk=board.pk)


@login_required
def board_archive(request, pk):
    """«В архив»: `archive_board()`, refused while any card is still open."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method == 'POST':
        try:
            archive_board(board, actor=request.user)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Доска убрана в архив.')
    return _back_to_board(request, board)


@login_required
def board_restore(request, pk):
    """«Вернуть из архива»: `restore_board()`."""
    board = _board_or_404(pk)
    _require(can_restore_board(request.user, board))
    if request.method == 'POST':
        try:
            restore_board(board, actor=request.user)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Доска возвращена из архива.')
    return _back_to_board(request, board)


# --------------------------------------------------------------------------
# Sub-boards and columns
# --------------------------------------------------------------------------
#
# Small POST forms of the tabs and the column headers, drawn only for whoever
# manages the board. The right is asked before the method (a 403), a GET goes
# back to the sub-board and changes nothing, and a refusal — an invalid form
# or the service's `BoardError` — is a message on the sub-board it came from.
# Without JavaScript they are ordinary forms in a `<details>` menu; with it,
# the deletes ask through the shared confirmation modal first.


def _structure_request(request, pk, sub_pk=None):
    """The board (managed by the user) and the sub-board, or a redirect for a GET."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    sub_board = _sub_board_or_404(board, sub_pk) if sub_pk is not None else None
    return board, sub_board


def _back(board, sub_board_id, request=None):
    url = _sub_board_url(board, sub_board_id)
    query = parse_board_filters(request.GET).query if request is not None else ''
    return redirect(f'{url}?{query}' if query else url)


def _refused(request, form_or_error):
    if isinstance(form_or_error, str):
        messages.error(request, form_or_error)
        return
    for errors in form_or_error.errors.values():
        for error in errors:
            messages.error(request, error)


@login_required
def sub_board_create(request, pk, sub_pk):
    """«+» beside the tabs: a new sub-board. `sub_pk` is the tab it was asked
    from — where a refusal goes back to."""
    board, current = _structure_request(request, pk, sub_pk)
    if request.method != 'POST':
        return _back(board, current.pk, request)
    form = SubBoardNameForm(request.POST)
    if not form.is_valid():
        _refused(request, form)
        return _back(board, current.pk, request)
    try:
        sub_board = create_sub_board(board, actor=request.user, name=form.cleaned_data['name'])
    except BoardError as exc:
        _refused(request, str(exc))
        return _back(board, current.pk, request)
    messages.success(request, 'Поддоска создана.')
    return _back(board, sub_board.pk)


@login_required
def sub_board_rename(request, pk, sub_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    if request.method == 'POST':
        form = SubBoardNameForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                rename_sub_board(sub_board, actor=request.user, name=form.cleaned_data['name'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back(board, sub_board.pk, request)


@login_required
def sub_board_move(request, pk, sub_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    if request.method == 'POST':
        form = DirectionForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                move_sub_board(sub_board, actor=request.user, direction=form.cleaned_data['direction'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back(board, sub_board.pk, request)


@login_required
def sub_board_delete(request, pk, sub_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    if request.method != 'POST':
        return _back(board, sub_board.pk, request)
    try:
        delete_sub_board(sub_board, actor=request.user)
    except BoardError as exc:
        _refused(request, str(exc))
        return _back(board, sub_board.pk, request)
    messages.success(request, 'Поддоска удалена.')
    return redirect('boards:detail', pk=board.pk)


@login_required
def column_create(request, pk, sub_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    if request.method == 'POST':
        form = ColumnNameForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                create_column(sub_board, actor=request.user, name=form.cleaned_data['name'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back(board, sub_board.pk, request)


def _column_or_404(sub_board, column_pk):
    return get_object_or_404(BoardColumn.objects.select_related('sub_board'), pk=column_pk, sub_board=sub_board)


@login_required
def column_rename(request, pk, sub_pk, column_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    column = _column_or_404(sub_board, column_pk)
    if request.method == 'POST':
        form = ColumnNameForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                rename_column(column, actor=request.user, name=form.cleaned_data['name'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back(board, sub_board.pk, request)


@login_required
def column_move(request, pk, sub_pk, column_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    column = _column_or_404(sub_board, column_pk)
    if request.method == 'POST':
        form = DirectionForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                move_column(column, actor=request.user, direction=form.cleaned_data['direction'])
            except BoardError as exc:
                _refused(request, str(exc))
    return _back(board, sub_board.pk, request)


@login_required
def column_delete(request, pk, sub_pk, column_pk):
    board, sub_board = _structure_request(request, pk, sub_pk)
    column = _column_or_404(sub_board, column_pk)
    if request.method == 'POST':
        try:
            delete_column(column, actor=request.user)
        except BoardError as exc:
            _refused(request, str(exc))
        else:
            messages.success(request, 'Колонка удалена.')
    return _back(board, sub_board.pk, request)


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------


@login_required
def board_members(request, pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    return _render_members(request, board)


def _render_members(request, board, *, posted=(), extra_row=False):
    """«Участники»: the list, and for the owner or an administrator the picker rows.

    The rows offer every active employee who is not on the board yet — the
    current members are the picker's `exclude`.
    """
    can_manage = can_manage_board(request.user, board)
    members = (
        BoardMember.objects.filter(board=board)
        .select_related('user__userprofile__department')
        .order_by('user__last_name', 'user__first_name', 'user__username')
    )
    context = {
        **_frame(request, board),
        'header_title': f'Участники · {board.name}',
        'board': board,
        'members': members,
        'can_manage': can_manage,
    }
    if can_manage:
        context.update(_member_picker(
            posted, extra_row=extra_row, exclude=[member.user_id for member in members],
        ))
    return render(request, 'boards/members.html', context)


@login_required
def members_add(request, pk):
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method != 'POST':
        return redirect('boards:members', pk=board.pk)
    if 'add_member_row' in request.POST:
        # «+ Добавить участника» without JavaScript: one more row, nothing added.
        return _render_members(
            request, board, posted=request.POST.getlist('users'), extra_row=True,
        )
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
