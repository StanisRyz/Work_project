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
`/work/boards/<board>/<sub_board>/` is the page. The card drawer is part of
it, chosen by the query string: `?card=<pk>` reads a card, `&edit=1` edits it,
`?new=<column id>` creates one in that column, `&tab=` names the drawer's
tab («Описание», «Чат», «Подзадачи» — a card's, never a subtask's — «Лог»;
the old `files` is «Чат» showing «Только файлы», `&chat=files`). A subtask
is opened the same way, `?card=<its pk>`, on its card's sub-board. A refused or invalid POST renders the
sub-board again with the drawer open, the typed values in place and the error
beside the form; a successful one redirects to the card's sub-board with the
card open.

The live blocks — the tabs, the columns, the card's guarded panel, its chat,
its log, its checklist, its followers and its «Подзадачи» — are rendered by `boards:fragment` through the very same context
builder and the same partials as the page, so a refreshed block cannot
disagree with a reload; the same answer is what `board_drawer.js` opens a
card with, without reloading the page.

The structure routes (tabs and columns: create, rename, ←/→, delete) are small
POST forms for whoever manages the board; a refusal comes back as a message on
the sub-board.

The files of «Чат» (`BoardCardFile`) are posted with a message and served
only by `file_download`/`file_preview`, which ask reading the board again;
`MEDIA_ROOT` is never published.
"""

import logging
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.middleware.csrf import get_token
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET

from accounts.directory import get_employee_directory
from accounts.templatetags.people import person_name
from ecosystem.templatetags.registry import plural_ru
from ecosystem.xlsx import xlsx_response
from realtime.auth import realtime_login_required
from realtime.fragments import content_revision
from ecosystem.logging_utils import log_event
from tasks.permissions import can_reopen_task

from .forms import (
    AddMembersForm,
    BoardCodeForm,
    BoardForm,
    BoardNameForm,
    CardForm,
    ColumnNameForm,
    FieldForm,
    FieldUpdateForm,
    OptionForm,
    ColumnPinsForm,
    ColumnStaleForm,
    DirectionForm,
    MoveCardForm,
    SubBoardNameForm,
    SubtaskForm,
    custom_field_name,
)
from .models import (
    MAX_FILES_PER_MESSAGE,
    PREVIEW_IMAGE_TYPES,
    Board,
    BoardCard,
    BoardCardChecklistItem,
    BoardCardFile,
    BoardCardFieldValue,
    BoardColumn,
    BoardField,
    BoardFieldColor,
    BoardFieldOption,
    BoardMember,
    SubBoard,
)
from .permissions import (
    can_cancel_card,
    can_comment_card,
    can_create_board,
    can_delete_card_file,
    can_manage_board,
    can_restore_board,
    can_view_board,
    can_work_on_board,
)
from .selectors import (
    NO_FILTERS,
    board_fields,
    DEVIATION_DEFAULT_DAYS,
    board_tabs,
    build_board_nav,
    build_deviation_report,
    build_board_state,
    build_board_table,
    checklist_counts,
    column_counts,
    describe_field_filters,
    first_sub_board,
    member_preview,
    names_field_filter,
    number_input,
    open_subtasks_warning,
    parse_board_filters,
    parse_table_sort,
    resolve_new_column,
)
from .services import (
    MAX_FIELDS,
    MAX_OPTIONS,
    BoardCodeError,
    BoardError,
    DueReasonError,
    FieldValueError,
    StaleCardError,
    add_board_members,
    add_checklist_item,
    checklist_item_to_subtask,
    create_subtask,
    create_subtasks_from_list,
    delete_checklist_item,
    move_checklist_item,
    rename_checklist_item,
    toggle_card_subscription,
    toggle_checklist_item,
    archive_field,
    archive_option,
    create_field,
    create_option,
    delete_field,
    delete_option,
    move_field,
    move_option,
    restore_field,
    restore_option,
    update_field,
    update_option,
    archive_board,
    cancel_card,
    change_board_code,
    complete_card,
    create_board,
    create_card,
    create_column,
    create_sub_board,
    delete_card_file,
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
    reopen_card,
    restore_board,
    set_column_pins,
    set_column_stale_days,
    update_card,
)


def _board_or_404(pk):
    return get_object_or_404(Board.objects.select_related('department', 'owner'), pk=pk)


def _sub_board_or_404(board, sub_pk):
    return get_object_or_404(SubBoard, pk=sub_pk, board=board)


def _sub_board_url(board, sub_board_id):
    return reverse('boards:sub_board', args=[board.pk, sub_board_id])


def _request_filters(request, board, fields=None):
    """The board filters of `request.GET` — `parse_board_filters()` with the
    board's own fields.

    `fields` already read are used as they are; otherwise the fields are read
    only when the query names a field filter (`f_…`), so a redirect or a
    drag's answer under «Мои» or the search costs no query more.
    """
    if fields is None:
        fields = board_fields(board) if names_field_filter(request.GET) else ()
    return parse_board_filters(request.GET, fields)


def _card_url(board, card, request=None, *, tab=''):
    """The card's sub-board with `card` open — and, from a request, the filter
    it was under; `tab` opens the panel on that tab («Чат» after a message).

    Every board form posts to a URL carrying the board's filter query, so the
    redirect after it lands on the same filtered sub-board.
    """
    url = f'{_sub_board_url(board, card.sub_board_id)}?card={card.pk}'
    if tab:
        url = f'{url}&tab={tab}'
    query = _request_filters(request, board).query if request is not None else ''
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
            form = BoardForm(
                initial={'name': request.POST.get('name', ''), 'code': request.POST.get('code', '')},
                owner=request.user,
            )
        elif form.is_valid():
            try:
                board = create_board(
                    name=form.cleaned_data['name'],
                    code=form.cleaned_data['code'],
                    owner=request.user,
                    actor=request.user,
                    member_ids=[user.pk for user in form.cleaned_data['members']],
                )
            except BoardCodeError as exc:
                form.add_error('code', str(exc))
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


def _card_initial(item, fields):
    initial = {
        'title': item['card'].title,
        'description': item['card'].description,
        'due_date': item['due_date'],
        'assignees': [user.pk for user in item['assignees']],
        'version': item['card'].version,
    }
    for field in fields:
        row = item['field_rows'].get(field.pk)
        if row is None or field.is_archived:
            continue
        if field.kind == BoardField.Kind.SELECT:
            value = str(row.option_id)
        elif field.kind == BoardField.Kind.NUMBER:
            value = number_input(row.value_number)
        elif field.kind == BoardField.Kind.DATE:
            value = row.value_date
        else:
            value = row.value_text
        initial[custom_field_name(field)] = value
    return initial


def _current_field_rows(card):
    """`{field id: value row}` of a card — what its edit form offers again."""
    return {row.field_id: row for row in BoardCardFieldValue.objects.filter(card=card)}


def _field_error(form, exc):
    """A refused field value beside its own input — a refused move of the
    срок beside «Причина переноса»; anything else for the panel."""
    if isinstance(exc, DueReasonError) and 'due_reason' in form.fields:
        form.add_error('due_reason', str(exc))
        return ''
    if isinstance(exc, FieldValueError) and custom_field_name(exc.field_id) in form.fields:
        form.add_error(custom_field_name(exc.field_id), str(exc))
        return ''
    return str(exc)


# The files of «Чат» are logged as attachments are: identifiers and sizes.
file_logger = logging.getLogger('ecosystem.attachments')

TABS_TEMPLATE = 'boards/includes/tabs.html'
COLUMNS_TEMPLATE = 'boards/includes/columns.html'
PANEL_TEMPLATE = 'boards/includes/panel.html'
CARD_TEMPLATE = 'boards/includes/card.html'
COMMENTS_TEMPLATE = 'boards/includes/comments.html'
LOG_TEMPLATE = 'boards/includes/log.html'
CHECKLIST_TEMPLATE = 'boards/includes/checklist.html'
FACTS_TEMPLATE = 'boards/includes/facts.html'
FOLLOWERS_TEMPLATE = 'boards/includes/followers.html'
SUBTASKS_TEMPLATE = 'boards/includes/subtasks.html'
SUBTASK_SUMMARY_TEMPLATE = 'boards/includes/subtask_summary.html'
SUBTASK_WARNING_TEMPLATE = 'boards/includes/subtask_warning.html'
DRAWER_TEMPLATE = 'boards/includes/drawer.html'

# The card panel's tabs, in order: `?tab=` names one, anything else is the
# first. «Чат» shows its number beside the name, «Подзадачи» «done/total».
# A subtask has no «Подзадачи» of its own: one level only.
PANEL_TABS = (
    ('description', 'Описание'),
    ('chat', 'Чат'),
    ('subtasks', 'Подзадачи'),
    ('log', 'Лог'),
)
SUBTASKS_TAB = 'subtasks'
# «Скрыть выполненные» of «Подзадачи»: `&subtasks_done=hide`, drawn by the server.
SUBTASKS_HIDE_DONE = 'hide'
DEFAULT_PANEL_TAB = PANEL_TABS[0][0]
# «Чат» shows every message, or — `&chat=files` — only the card's files.
CHAT_MESSAGES = 'messages'
CHAT_FILES = 'files'
# The tab «Файлы» is gone; its address — every link and notification that
# still says `tab=files` — is «Чат» showing «Только файлы».
FILES_TAB_ALIAS = 'files'


def parse_panel_tab(value):
    """The tab `?tab=` names, or «Описание» for anything unknown; the old
    `tab=files` is «Чат»."""
    if value == FILES_TAB_ALIAS:
        return 'chat'
    return value if value in dict(PANEL_TABS) else DEFAULT_PANEL_TAB


def parse_chat_mode(params):
    """«Только файлы» (`chat=files`, or the old `tab=files`) or every message."""
    if params.get('chat') == CHAT_FILES or params.get('tab') == FILES_TAB_ALIAS:
        return CHAT_FILES
    return CHAT_MESSAGES


def _board_context(request, board, sub_board, *, card_id=None, edit=False, new=None, panel=None,
                   form=None, move_form=None, error='', execution_comment=None,
                   execution_error='', version_conflict=False,
                   comment_text='', comment_error='', comment_mentions=(),
                   checklist_text='', checklist_error='', edit_item=None,
                   checklist_edit_text='', checklist_edit_error='', tabs=None,
                   subtask_form=None, subtask_error='', subtask_list_text='', subtask_list_error='',
                   tab=None):
    """Everything the board page and its live fragment render.

    `panel` is `'view'`, `'edit'` or `'new'`; `None` decides it from `card_id`,
    `edit` and `new` — the query string's `card`, `edit=1` and `new` (a
    working column's id). A form
    passed in is shown as it is — bound, with its errors — so a refused POST
    keeps what was typed.

    «Завершить» in the panel's heading starts from what was just posted (a
    refused completion), else from the task's own result — so a task an
    administrator reopened shows what was written before. Nothing is taken
    from the session: the board carries no «Выполнение» draft.

    `?tab=` (`parse_panel_tab()`) says which of the panel's tabs is shown. It
    changes no block — all three are drawn, the tab is an attribute of the
    drawer around them — only the addresses the page builds for itself.
    `&chat=files` (`parse_chat_mode()`; the old `tab=files` too) shows «Чат»
    as «Только файлы»: an attribute of the chat's section likewise, both
    lists being in its block.

    `tabs` are the board's sub-boards when the view has read them already.

    «Подзадачи» (a card's, never a subtask's) are a tab of their own: the
    list is a read-only live block, «+ Подзадача» (`subtask_form`, bound after
    a refusal, with `subtask_error`) and «Добавить списком»
    (`subtask_list_text`, `subtask_list_error`) are forms below it in no
    block. `&subtasks_done=hide` («Скрыть выполненные») is kept by every
    address the page builds for itself. `tab` forces the tab a refused POST
    comes back on.

    The filters (`?mine=1`, `?overdue=1`, `?q=` and the field filters
    `f_<id>…`) are read from `request.GET` here and nowhere else — on a POST
    too, whose action URL carries them — so the page, its fragment and a
    refused form are filtered alike, and every link the panel and the tiles
    draw keeps them (`filter_query`). The board's fields are read once, for
    the parse and for the page.

    `?edit_item=<id>` opens that item of the card's «Чек-лист» as a form
    (only while the list may be changed); the page's and the fragment's
    addresses keep it, so a reload and a live refresh draw the same block.
    A refused new item or a refused rename comes back with what was typed
    (`checklist_text`, `checklist_edit_text`) and the message beside it.

    Returns the context and whether the panel holds input that is not the
    stored state (a bound form, posted or parked text): such a page starts
    «dirty» for the live client, and its panel fingerprint comes from a clean
    render.
    """
    fields = board_fields(board)
    filters = _request_filters(request, board, fields)
    all_comments = request.GET.get('comments') == 'all'
    state = build_board_state(
        board, sub_board, request.user,
        card_id=card_id, filters=filters, all_comments=all_comments, fields=fields, tabs=tabs,
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
        form = CardForm(
            board=board, fields=state['fields'], field_rows=item['field_rows'],
            initial=_card_initial(item, state['fields']), editing=True,
        )
    if panel == 'new' and form is None:
        form = CardForm(board=board, fields=state['fields'], initial={'column': new_column.pk})
    if panel == 'edit':
        # The stored срок: `board_due.js` shows «Причина переноса» while the
        # date in the form differs from it.
        form.fields['due_date'].widget.attrs['data-stored-due'] = item['due_date'].isoformat()
        if item['is_subtask']:
            # A subtask's срок is checked against its card's by the page's warning.
            form.fields['due_date'].widget.attrs['data-subtask-due'] = ''
    # A subtask lives inside its card: it is never moved to a column.
    if panel == 'view' and can_edit_card and move_form is None and not item['is_subtask']:
        move_form = MoveCardForm(
            initial={'column_id': item['column'].pk if item['column'] else None},
        )
    if move_form is not None:
        # The select offers the working columns of every sub-board of the
        # board, grouped by sub-board, this one first.
        move_form.fields['column_id'].widget.choices = state['move_choices']
    if item is not None and item['can_complete'] and execution_comment is None:
        execution_comment = item['task'].execution_comment
    tab = parse_panel_tab(tab if tab is not None else request.GET.get('tab'))
    is_subtask = bool(item and item['is_subtask'])
    if tab == SUBTASKS_TAB and is_subtask:
        tab = DEFAULT_PANEL_TAB
    chat_mode = parse_chat_mode(request.GET)
    hide_done = request.GET.get('subtasks_done') == SUBTASKS_HIDE_DONE
    # «Изменить» of one item of the card's «Чек-лист»: an id of an item of
    # this very card, while the list may be changed — anything else is none.
    raw_edit_item = str(edit_item if edit_item is not None else request.GET.get('edit_item') or '')
    edit_item = None
    if item is not None and item['can_edit_checklist'] and raw_edit_item.isdigit():
        edit_item = next(
            (entry.pk for entry in item['checklist'] if entry.pk == int(raw_edit_item)), None,
        )
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
        'task': item['task'] if item else None,
        # Where `tasks:delete_attachment` sends the user back after removing
        # an older attachment of the card's task from «Чат»:
        # `tasks:detail?tab=chat`, which leads to this card's «Чат».
        'list_query': 'tab=chat',
        'max_files_per_message': MAX_FILES_PER_MESSAGE,
        'execution_comment': execution_comment or '',
        'execution_error': execution_error,
        'board_url': board_url,
        'filter_query': filters.query,
        'filter_suffix': f'?{filters.query}' if filters.query else '',
        'version_conflict': version_conflict,
        # The tabs «Чат» and «Лог» exist for a card being read or edited;
        # a new card has only its form. The chat's form is outside every live
        # block, so a refresh never redraws what is being typed in it.
        'show_discussion': bool(item is not None and panel in ('view', 'edit')),
        'comment_text': comment_text,
        'comment_error': comment_error,
        # The people ticked under «Упомянуть» of a refused message, as posted.
        'comment_mentions': [str(value) for value in comment_mentions],
        # «Чек-лист» lives on «Описание» of a card being read or edited: its
        # items are a live block of their own, the field adding one is in
        # none.
        'show_checklist': bool(item is not None and panel in ('view', 'edit')),
        'checklist_edit_item': edit_item,
        'checklist_text': checklist_text,
        'checklist_error': checklist_error,
        'checklist_edit_text': checklist_edit_text,
        'checklist_edit_error': checklist_edit_error,
        'tab': tab if item is not None and panel in ('view', 'edit') else DEFAULT_PANEL_TAB,
        'chat_mode': chat_mode if item is not None and panel in ('view', 'edit') else CHAT_MESSAGES,
        # «Подзадачи» belong to a card being read or edited, never to a
        # subtask. Their forms are below the live list, in no block.
        'show_subtasks': bool(item is not None and panel in ('view', 'edit') and not is_subtask),
        'subtasks_hide_done': hide_done,
        'subtask_form': subtask_form,
        'subtask_error': subtask_error,
        'subtask_list_text': subtask_list_text,
        'subtask_list_error': subtask_list_error,
        # «Открыто подзадач: 2 (ZAP-13, ZAP-15).» — what «Завершить» and
        # «Отменить карточку» of a card with open subtasks warn about.
        'subtask_warning_text': open_subtasks_warning(
            row['card'].code for row in (item['subtask_open_rows'] if item else ())
        ),
    })
    if state['show_subtasks'] and item['can_add_subtask'] and subtask_form is None:
        # The card's own исполнители and срок, as the form starts.
        state['subtask_form'] = SubtaskForm(board=board, members=item['members'], initial={
            'due_date': item['due_date'],
            'assignees': [user.pk for user in item['assignees']],
        })
    fragment_base = reverse('boards:fragment', args=[board.pk, sub_board.pk])
    query = _panel_query(
        item, panel, new_column, filters, all_comments=all_comments, tab=state['tab'], edit_item=edit_item,
        chat_mode=state['chat_mode'], hide_done=hide_done,
    )
    state['fragment_base'] = fragment_base
    state['fragment_url'] = fragment_base + query
    state['page_url'] = board_url + query
    # «Доска | Таблица» and «Excel» of the filter row: the table of this
    # sub-board under the same filter, and its spreadsheet.
    state.update(_view_switch(board_url, filters.query))
    # Closing the panel: the sub-board under the same filter.
    state['close_url'] = board_url + state['filter_suffix']
    state['close_fragment_url'] = fragment_base + state['filter_suffix']
    # «Сбросить» of the filter row keeps the open card and drops the filter.
    state['reset_url'] = (
        f'{board_url}?card={item["card"].pk}' if item is not None and panel == 'view' else board_url
    )
    # The «Поля» panel of the filter row and the chips under it. A chip's «×»
    # is what the filter form itself would ask for without that field: the
    # open card and its tab (as the form's hidden fields carry them), the
    # rest of the filter — `board_drawer.js` keeps the card and the tab
    # current, as it does for «Сбросить».
    open_card = (
        urlencode({'card': item['card'].pk, 'tab': state['tab']})
        if item is not None and panel == 'view' else ''
    )
    field_filters = describe_field_filters(state['fields'], filters)
    for row in field_filters:
        if row['filter'] is not None:
            rest = filters.without_field(row['field'].pk).query
            encoded = '&'.join(part for part in (open_card, rest) if part)
            row['remove_url'] = f'{board_url}?{encoded}' if encoded else board_url
    state['field_filters'] = field_filters
    state['field_filter_count'] = len(filters.fields)
    def panel_query(**options):
        values = {
            'all_comments': all_comments, 'tab': state['tab'], 'edit_item': edit_item,
            'chat_mode': state['chat_mode'], 'hide_done': hide_done, **options,
        }
        return _panel_query(item, panel, new_column, filters, **values)

    def tab_count(name):
        if name == 'chat':
            return item['comment_count']
        if name == SUBTASKS_TAB:
            return subtasks_count_label(item)
        return None

    # The tab strip: each tab's own page and fragment address, built here.
    state['panel_tabs'] = [
        {
            'name': name,
            'label': label,
            'active': name == state['tab'],
            'page_url': board_url + panel_query(tab=name),
            'fragment_url': fragment_base + panel_query(tab=name),
            'count': tab_count(name),
        }
        for name, label in PANEL_TABS
        if name != SUBTASKS_TAB or state['show_subtasks']
    ] if state['show_discussion'] else []
    # «Подзадачи»: the tab's own address (the line «Подзадачи: 1 из 3» on
    # «Описание» leads there), and «Скрыть выполненные» / «Показать
    # выполненные» — the same panel with the other `subtasks_done`.
    if state['show_subtasks']:
        state['subtasks_tab_url'] = board_url + panel_query(tab=SUBTASKS_TAB)
        state['subtasks_toggle_url'] = board_url + panel_query(tab=SUBTASKS_TAB, hide_done=not hide_done)
    # «Все сообщения | Только файлы» above the chat: each mode's page and
    # fragment address, on «Чат» — switched in place by `board_drawer.js`,
    # links without it.
    state['chat_modes'] = [
        {
            'name': name,
            'label': label,
            'active': name == state['chat_mode'],
            'page_url': board_url + panel_query(tab='chat', chat_mode=name),
            'fragment_url': fragment_base + panel_query(tab='chat', chat_mode=name),
        }
        for name, label in ((CHAT_MESSAGES, 'Все сообщения'), (CHAT_FILES, 'Только файлы'))
    ] if state['show_discussion'] else []
    # «Показать ранние (N)»: this very panel with every message, on «Чат».
    state['all_comments_url'] = (
        board_url + panel_query(all_comments=True, tab='chat', chat_mode=CHAT_MESSAGES)
        if state['show_discussion'] else ''
    )
    # «К сообщению» of «Только файлы»: the chat's messages — every one when
    # the message is older than those shown — and the anchor of the row.
    state['chat_messages_url'] = (
        board_url + panel_query(tab='chat', chat_mode=CHAT_MESSAGES) if state['show_discussion'] else ''
    )
    return state, holds_input


def subtasks_count_label(item):
    """«1/3» beside «Подзадачи»: done of total (cancelled ones are no work)."""
    return f"{item['subtask_done']}/{item['subtask_total']}"


def _panel_query(item, panel, new_column, filters, *, all_comments=False, tab=DEFAULT_PANEL_TAB,
                 edit_item=None, chat_mode=CHAT_MESSAGES, hide_done=False):
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
        if all_comments:
            query['comments'] = 'all'
        query['tab'] = tab
        if tab == 'chat' and chat_mode == CHAT_FILES:
            query['chat'] = CHAT_FILES
        if edit_item is not None:
            query['edit_item'] = edit_item
        if hide_done:
            query['subtasks_done'] = SUBTASKS_HIDE_DONE
    elif panel == 'new' and new_column is not None:
        query['new'] = new_column.pk
    encoded = '&'.join(part for part in (urlencode(query), filters.query) if part)
    return f'?{encoded}' if encoded else ''


def _render_block(template, context, request):
    """One live block's markup — without running the context processors again.

    `render_to_string(…, request=request)` builds a `RequestContext`, which
    runs every context processor of the project — the bell's two queries
    among them — for each of the seven blocks of a sub-board, although no
    block draws the bell or the menu. The blocks need only what a request
    gives a template: `request`, `user` and the CSRF token their forms carry;
    the page itself is rendered once, with the processors, around them.
    """
    return render_to_string(template, {
        **context,
        'request': request,
        'user': request.user,
        'csrf_token': get_token(request),
    })


def _board_blocks(request, context):
    """The live blocks as markup, each with its fingerprint, and the drawer.

    The tabs and the columns are read-only blocks of their own (the filter row
    stands between them on the page), so a tab or a column created, renamed,
    moved or deleted by somebody else arrives with the cards.

    The card panel is one guarded block in three containers — its heading
    (`panel_html`: number, title, status, «Завершить», «Вернуть в работу»,
    «Следить», «×»), «Описание»'s text or the card's form (`card_html`), and
    its facts, result and tools (`facts_html`) — with one fingerprint over
    all three, `panel_revision`. The messages of «Чат» (with their files) and
    the entries of «Лог» are read-only blocks of their own and appear in no
    other: a new message, a file deleted or a new entry moves
    `comments_revision`/`log_revision`, never `panel_revision`, so neither
    can raise the conflict banner over a result or an edit being typed.
    `chat_count`/`files_count` are the numbers beside «Чат» and «Файлы · N»,
    for the client to set with their block.

    The card's «Чек-лист» (`checklist_html`, on «Описание», between the text
    and the facts) is a block of its own too, outside the guarded one:
    ticking an item moves `checklist_revision` and the columns' (the tile's
    «☑ 2/5»), never `panel_revision`. So are its «Подписчики»
    (`followers_html`, below the facts): somebody starting to follow — a
    colleague mentioned in «Чат» does — moves `followers_revision` alone.

    `drawer_html` is the whole drawer around those blocks — the tab strip and
    the chat's and the checklist's forms included — which the client inserts
    once when it opens a card without reloading the page.
    """
    tabs_html = _render_block(TABS_TEMPLATE, context, request)
    columns_html = _render_block(COLUMNS_TEMPLATE, context, request)
    panel = context['panel']
    panel_html = _render_block(PANEL_TEMPLATE, context, request) if panel else ''
    card_html = _render_block(CARD_TEMPLATE, context, request) if panel else ''
    facts_html = _render_block(FACTS_TEMPLATE, context, request) if panel == 'view' else ''
    discussion = context['show_discussion']
    comments_html = _render_block(COMMENTS_TEMPLATE, context, request) if discussion else ''
    log_html = _render_block(LOG_TEMPLATE, context, request) if discussion else ''
    checklist = context['show_checklist']
    checklist_html = _render_block(CHECKLIST_TEMPLATE, context, request) if checklist else ''
    followers = panel == 'view'
    followers_html = _render_block(FOLLOWERS_TEMPLATE, context, request) if followers else ''
    item = context['card']
    # «Подзадачи»: the list (its tab), the line on «Описание» and the warning
    # of «Завершить» — one read-only block in three containers with one
    # fingerprint, outside the guarded panel. A subtask completed by somebody
    # else moves `subtasks_revision` and never `panel_revision`: the guarded
    # fingerprint is taken from a render with the warning left out, and the
    # client sets the warning itself with the block.
    subtasks = context['show_subtasks']
    subtasks_html = _render_block(SUBTASKS_TEMPLATE, context, request) if subtasks else ''
    subtask_summary_html = _render_block(SUBTASK_SUMMARY_TEMPLATE, context, request) if subtasks else ''
    subtask_warning_html = _render_block(SUBTASK_WARNING_TEMPLATE, context, request) if subtasks else ''
    panel_revision = content_revision(panel_html + card_html + facts_html) if panel else ''
    if panel and item is not None and context['subtask_warning_text']:
        quiet = {**context, 'subtask_fingerprint': True}
        panel_revision = content_revision(
            _render_block(PANEL_TEMPLATE, quiet, request)
            + card_html
            + (_render_block(FACTS_TEMPLATE, quiet, request) if panel == 'view' else '')
        )
    blocks = {
        'tabs_html': tabs_html,
        'tabs_revision': content_revision(tabs_html),
        'columns_html': columns_html,
        'columns_revision': content_revision(columns_html),
        'panel_html': panel_html,
        'card_html': card_html,
        'facts_html': facts_html,
        'panel_revision': panel_revision,
        'comments_html': comments_html,
        'comments_revision': content_revision(comments_html) if comments_html else '',
        'log_html': log_html,
        'log_revision': content_revision(log_html) if log_html else '',
        # Always a fingerprint while the list is shown — an emptied list is a
        # change too.
        'checklist_html': checklist_html,
        'checklist_revision': content_revision(checklist_html) if checklist else '',
        # Likewise for «Подписчики»: an empty list is drawn as nothing, and
        # emptied is a change.
        'followers_html': followers_html,
        'followers_revision': content_revision(followers_html) if followers else '',
        'chat_count': item['comment_count'] if discussion else 0,
        'files_count': item['files_count'] if discussion else 0,
        'subtasks_html': subtasks_html,
        'subtask_summary_html': subtask_summary_html,
        'subtask_warning_html': subtask_warning_html,
        'subtask_warning_text': context['subtask_warning_text'] if subtasks else '',
        'subtasks_revision': (
            content_revision(subtasks_html + subtask_summary_html + subtask_warning_html) if subtasks else ''
        ),
        'subtasks_count': subtasks_count_label(item) if subtasks else '',
        'subtasks_total': item['subtask_total'] if subtasks else 0,
    }
    blocks['drawer_html'] = (
        _render_block(DRAWER_TEMPLATE, {**context, **blocks}, request) if panel else ''
    )
    return blocks


def _member_heading(board):
    """The heading's member avatars and their number — one query."""
    members, count = member_preview(board)
    return {
        'member_preview': members,
        'member_count': count,
        'member_more': max(count - len(members), 0),
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
            tabs=options.get('tabs'),
        )
        blocks['panel_revision'] = _board_blocks(request, clean)['panel_revision']
        # The drawer printed on the page carries the bound form, not the clean one.
        blocks['drawer_html'] = _render_block(DRAWER_TEMPLATE, {**context, **blocks}, request)
    context.update(blocks)
    context['panel_holds_input'] = holds_input
    # The frame and the heading are the page's only — never part of a
    # fragment, so a live refresh never pays for them.
    context.update(_frame(request, board))
    context.update(_member_heading(board))
    request.session[LAST_SUB_BOARD_SESSION_KEY] = sub_board.pk
    return render(request, 'boards/detail.html', context, status=status)


# --------------------------------------------------------------------------
# «Таблица» and its Excel
# --------------------------------------------------------------------------
#
# `?view=table` on a sub-board's own address: the same cards as rows, every
# live field of the board a column, sortable by a whitelist
# (`selectors.parse_table_sort()`), filtered by the board's own filters;
# `&export=xlsx` is that very state as a spreadsheet. Read only: reading the
# board is the right, and nothing here writes. Not live — «Обновить» is a link.

TABLE_VIEW = 'table'


def _view_switch(board_url, filter_query):
    """The addresses of «Доска | Таблица» and «Excel» under `filter_query`."""
    table_query = '&'.join(part for part in (f'view={TABLE_VIEW}', filter_query) if part)
    return {
        'board_view_url': f'{board_url}?{filter_query}' if filter_query else board_url,
        'table_view_url': f'{board_url}?{table_query}',
        'export_url': f'{board_url}?{table_query}&export=xlsx',
    }


def _table_query(filters, *, cancelled, whole_board, sort, subtasks=False):
    """The table's own query string, without `?`: the view, the board's
    filters, «Отменённые», «Все поддоски», «Подзадачи» and the order — in a
    fixed order."""
    params = [('view', TABLE_VIEW)]
    query = filters.query
    if cancelled:
        params.append(('cancelled', '1'))
    if whole_board:
        params.append(('scope', 'board'))
    if subtasks:
        params.append(('subtasks', '1'))
    if sort:
        params.append(('sort', sort))
    encoded = urlencode(params)
    return '&'.join(part for part in (encoded, query) if part)


def _table_options(request):
    """«Отменённые», «Все поддоски» and «Подзадачи» as the address says them."""
    return {
        'cancelled': request.GET.get('cancelled') == '1',
        'whole_board': request.GET.get('scope') == 'board',
        'subtasks': request.GET.get('subtasks') == '1',
    }


def _render_table(request, board, sub_board):
    """The table of a sub-board (or of the whole board), or its `.xlsx`."""
    fields = board_fields(board)
    filters = parse_board_filters(request.GET, fields)
    options = _table_options(request)
    sort = parse_table_sort(request.GET.get('sort'), fields)
    state = build_board_table(
        board, sub_board, request.user, filters=filters, sort=sort, fields=fields, **options,
    )
    if request.GET.get('export') == 'xlsx':
        return _export_table(state)
    board_url = _sub_board_url(board, sub_board.pk)
    query = _table_query(filters, sort=sort, **options)
    context = {
        **state,
        **_frame(request, board),
        **_view_switch(board_url, filters.query),
        'active_page': 'boards',
        'header_title': board.name,
        'view': TABLE_VIEW,
        'board_url': board_url,
        'filter_query': filters.query,
        'filter_suffix': f'?{filters.query}' if filters.query else '',
        # The tabs lead to the other sub-boards' tables, the same options kept.
        'tab_suffix': f'?{query}',
        'refresh_url': f'{board_url}?{query}',
        # «Сбросить» drops the board's filters and keeps the table's own
        # options — «Отменённые», «Все поддоски» and the order.
        'reset_url': f'{board_url}?{_table_query(NO_FILTERS, sort=sort, **options)}',
        'generated_at': timezone.localtime(),
        'rows_label': _rows_label(state['rows']),
        'field_filters': describe_field_filters(fields, filters),
        'field_filter_count': len(filters.fields),
        'can_work': can_work_on_board(request.user, board),
        'can_manage': can_manage_board(request.user, board),
        'can_restore': can_restore_board(request.user, board),
        **_member_heading(board),
        'panel': None,
    }
    for row in context['field_filters']:
        if row['filter'] is not None:
            rest = _table_query(filters.without_field(row['field'].pk), sort=sort, **options)
            row['remove_url'] = f'{board_url}?{rest}'
    request.session[LAST_SUB_BOARD_SESSION_KEY] = sub_board.pk
    return render(request, 'boards/table.html', context)


def _rows_label(rows):
    """«8 карточек» — and «и 4 подзадачи» when the table shows them."""
    subtasks = sum(1 for row in rows if row.get('is_subtask'))
    cards = len(rows) - subtasks
    label = f"{cards} {plural_ru(cards, 'карточка', 'карточки', 'карточек')}"
    if subtasks:
        label += f" и {subtasks} {plural_ru(subtasks, 'подзадача', 'подзадачи', 'подзадач')}"
    return label


def table_headers(state):
    """The header row of the table's spreadsheet."""
    return [
        'Код',
        *(['Поддоска'] if state['whole_board'] else []),
        *(['Родитель'] if state['subtasks'] else []),
        'Название',
        'Колонка',
        'Статус',
        'Исполнители',
        'Срок',
        'Исходный срок',
        'Переносов',
        'Последняя причина',
        'В колонке, дн.',
        'Чек-лист',
        *(field.name for field in state['field_columns']),
        'Создана',
        'Завершена',
    ]


def table_cells(state, row):
    """One row of the spreadsheet: values as Excel takes them, `None` for empty."""
    return [
        row['card'].code,
        *([row['sub_board'].name if row['sub_board'] else None] if state['whole_board'] else []),
        *([row['parent_code'] or None] if state['subtasks'] else []),
        row['card'].title,
        row['column_label'] or None,
        row['status_label'],
        ', '.join(person_name(user) for user in row['assignees']) or None,
        row['due_date'],
        row['original_due_date'],
        row['due_change_count'],
        row['last_due_reason'] or None,
        row['in_column_days'],
        row['checklist_label'] or None,
        *(cell['raw'] for cell in row['cells']),
        row['created'],
        row['completed'],
    ]


# Russian letters as they are spelled in Latin in a file name: the name of a
# download stays ASCII («ZAP-Osnovnaya-2026-10-06.xlsx»), as `ecosystem.xlsx`
# asks — an encoded Cyrillic name is what older browsers mangle.
_TRANSLIT = dict(zip(
    'абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    ['a', 'b', 'v', 'g', 'd', 'e', 'e', 'zh', 'z', 'i', 'y', 'k', 'l', 'm', 'n', 'o', 'p',
     'r', 's', 't', 'u', 'f', 'kh', 'ts', 'ch', 'sh', 'shch', '', 'y', '', 'e', 'yu', 'ya'],
))


def safe_file_part(text):
    """`text` as a piece of a file name: Latin letters, digits and «-» only."""
    spelled = []
    for char in text:
        lower = char.lower()
        if lower in _TRANSLIT:
            latin = _TRANSLIT[lower]
            spelled.append(latin.capitalize() if char != lower else latin)
        elif char.isascii() and char.isalnum():
            spelled.append(char)
        else:
            spelled.append('-')
    return '-'.join(part for part in ''.join(spelled).split('-') if part)


def export_filename_stem(board, sub_board, *, whole_board):
    """`<код доски>-<поддоска>` — or `<код доски>-vse-poddoski` for the whole board."""
    second = 'vse-poddoski' if whole_board else safe_file_part(sub_board.name)
    return '-'.join(part for part in (safe_file_part(board.code), second) if part) or 'board'


def _export_table(state):
    """The table exactly as shown — the same rows, the same order, the same
    filters — as `<код доски>-<поддоска>-<дата>.xlsx`."""
    board, sub_board = state['board'], state['sub_board']
    return xlsx_response(
        export_filename_stem(board, sub_board, whole_board=state['whole_board']),
        board.name if state['whole_board'] else f'{board.code} {sub_board.name}',
        table_headers(state),
        [table_cells(state, row) for row in state['rows']],
        stamp_separator='-',
        typed_dates=True,
    )


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


def _tab_of(tabs, sub_pk):
    """The sub-board `sub_pk` names among the board's `tabs`, else `None` —
    read once for the page, which draws them all anyway."""
    return next((tab for tab in tabs if tab.pk == sub_pk), None)


@login_required
def sub_board_detail(request, pk, sub_pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    if request.GET.get('view') == TABLE_VIEW:
        return _render_table(request, board, _sub_board_or_404(board, sub_pk))
    tabs = board_tabs(board)
    sub_board = _tab_of(tabs, sub_pk)
    if sub_board is None:
        raise Http404('Поддоска не найдена.')
    return _render_board(
        request, board, sub_board,
        card_id=request.GET.get('card'),
        edit=request.GET.get('edit') == '1',
        new=request.GET.get('new'),
        tabs=tabs,
    )


@realtime_login_required
@require_GET
def board_fragment(request, pk, sub_pk):
    """The live blocks of one sub-board, for the live client.

    The same right as the page, the same query string (`card`, `edit`, `new`,
    `tab`), the same context builder and the same partials: a refreshed block
    is what a reload would draw. JSON, never cached, nothing written. A
    sub-board deleted meanwhile is a 404, which ends the live client of that
    page.

    Besides the blocks it names the panel it drew (`panel`, `card_id`, `tab`)
    and the addresses the page keeps for it (`page_url`, `fragment_url`,
    `reset_url`), all built here: `board_drawer.js` opens a card with this
    very response, inserts `drawer_html` and puts `page_url` in the history.
    """
    board = _board_or_404(pk)
    if not can_view_board(request.user, board):
        return _no_cache(JsonResponse({'error': 'forbidden'}, status=403))
    tabs = board_tabs(board)
    sub_board = _tab_of(tabs, sub_pk)
    if sub_board is None:
        return _no_cache(JsonResponse({'error': 'not_found'}, status=404))
    context, _ = _board_context(
        request, board, sub_board,
        card_id=request.GET.get('card'),
        edit=request.GET.get('edit') == '1',
        new=request.GET.get('new'),
        tabs=tabs,
    )
    item = context['card']
    return _no_cache(JsonResponse({
        **_board_blocks(request, context),
        'panel': context['panel'] or '',
        'card_id': item['card'].pk if item is not None and context['panel'] else None,
        'task_id': item['task'].pk if item is not None and context['panel'] else None,
        'tab': context['tab'],
        'chat_mode': context['chat_mode'],
        'page_url': context['page_url'] if context['panel'] else context['close_url'],
        'fragment_url': context['fragment_url'],
        'reset_url': context['reset_url'],
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
                field_values=form.field_values(),
            )
        except BoardError as exc:
            return _render_board(
                request, board, sub_board, panel='new', form=form, error=_field_error(form, exc),
            )
        return redirect(_card_url(board, card, request))
    return _render_board(request, board, sub_board, panel='new', form=form)


@login_required
def card_update(request, pk, card_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
    form = CardForm(request.POST, board=board, field_rows=_current_field_rows(card), editing=True)
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
                field_values=form.field_values(),
                due_reason_id=form.cleaned_data['due_reason'] or None,
                due_comment=form.cleaned_data['due_comment'],
            )
        except BoardError as exc:
            # A stale version keeps the typed values in the edit panel and
            # offers the current card in another tab, so nothing typed is lost.
            return _render_board(
                request, board, card.sub_board, card_id=card.pk, panel='edit', form=form,
                error=_field_error(form, exc), version_conflict=isinstance(exc, StaleCardError),
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
        counts = column_counts(card.sub_board, request.user, _request_filters(request, board))
        return JsonResponse({'ok': True, 'column_id': card.column_id, 'counts': counts})
    return redirect(_card_url(board, card, request))


@login_required
def card_complete(request, pk, card_pk):
    """«Завершить» in the card panel's heading, or a drop on the closing column:
    `complete_card()`, i.e. `complete_task()` plus the card's journal entry.

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
    messages.success(
        request,
        'Подзадача выполнена.' if card.parent_id else 'Задача выполнена, карточка в завершающей колонке.',
    )
    return redirect(_card_url(board, card, request))


@login_required
def card_reopen(request, pk, card_pk):
    """«Вернуть в работу» in the card panel's heading: `reopen_card()`.

    The right is the task's own — `can_reopen_task()`: an administrator, a
    completed task — and it is asked before the method, so a typed-in URL
    without it is a 403 and a GET with it changes nothing. `reopen_card()`
    asks it again under the locks; a refusal (an archived board, a task
    reopened meanwhile) comes back as the panel with the message.
    """
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    card = get_object_or_404(BoardCard, pk=card_pk, board=board)
    task = card.tasks.select_related('status').first()  # `unique_board_card_task`
    _require(task is not None and can_reopen_task(task, request.user))
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
    try:
        reopen_card(card, actor=request.user)
    except BoardError as exc:
        return _render_board(request, board, card.sub_board, card_id=card.pk, panel='view', error=str(exc))
    messages.success(request, 'Задача возвращена в работу.')
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


# --------------------------------------------------------------------------
# «Подзадачи»
# --------------------------------------------------------------------------
#
# Ordinary POST forms on a card's «Подзадачи», so the tab works without
# JavaScript: the right — working on the board — is asked before the method,
# a GET goes back to the tab and changes nothing, and a refusal comes back as
# the panel on that tab with what was typed and the message beside the form.


def _subtask_request(request, pk, card_pk):
    board = _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    return board, card


@login_required
def subtask_create(request, pk, card_pk):
    """«+ Подзадача»: `create_subtask()` with the title, the исполнители and
    the срок of the form (which starts from the card's own)."""
    board, card = _subtask_request(request, pk, card_pk)
    if request.method != 'POST':
        return redirect(_card_url(board, card, request, tab=SUBTASKS_TAB))
    form = SubtaskForm(request.POST, board=board)
    if form.is_valid():
        try:
            subtask = create_subtask(
                card,
                actor=request.user,
                title=form.cleaned_data['title'],
                assignees=[user.pk for user in form.cleaned_data['assignees']],
                due_date=form.cleaned_data['due_date'],
            )
        except BoardError as exc:
            return _render_board(
                request, board, card.sub_board, card_id=card.pk, panel='view', tab=SUBTASKS_TAB,
                subtask_form=form, subtask_error=str(exc),
            )
        messages.success(request, f'Подзадача {subtask.code} добавлена.')
        return redirect(_card_url(board, card, request, tab=SUBTASKS_TAB))
    return _render_board(
        request, board, card.sub_board, card_id=card.pk, panel='view', tab=SUBTASKS_TAB, subtask_form=form,
    )


@login_required
def subtask_create_list(request, pk, card_pk):
    """«Добавить списком»: `create_subtasks_from_list()` — one subtask per
    line, all or none."""
    board, card = _subtask_request(request, pk, card_pk)
    if request.method != 'POST':
        return redirect(_card_url(board, card, request, tab=SUBTASKS_TAB))
    text = request.POST.get('text', '')
    try:
        subtasks = create_subtasks_from_list(card, actor=request.user, text=text)
    except BoardError as exc:
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view', tab=SUBTASKS_TAB,
            subtask_list_text=text, subtask_list_error=str(exc),
        )
    messages.success(
        request, f'Добавлено {len(subtasks)} {plural_ru(len(subtasks), "подзадача", "подзадачи", "подзадач")}.',
    )
    return redirect(_card_url(board, card, request, tab=SUBTASKS_TAB))


# Said beside a refused message that carried files: a browser never keeps a
# file input's choice across a page, so the files have to be chosen again.
CHOOSE_FILES_AGAIN = 'Выберите файлы заново.'


@login_required
def card_comment(request, pk, card_pk):
    """«Отправить» in the card's «Чат»: `post_card_comment()` — the text, the
    people «@» named and the files chosen (`files`, a multipart form).

    The right (`can_comment_card()`) is asked before the method. Success goes
    back to the card's «Чат» under the board's filter; a refusal re-renders
    the panel with the text and the message beside the form — and, when files
    were sent, «Выберите файлы заново»: the browser has dropped them.
    """
    board = _board_or_404(pk)
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    _require(can_comment_card(request.user, card))
    if request.method != 'POST':
        return redirect(_card_url(board, card, request))
    text = request.POST.get('text', '')
    mentions = request.POST.getlist('mention')
    files = request.FILES.getlist('files')
    try:
        post_card_comment(card, actor=request.user, text=text, mentions=mentions, files=files)
    except BoardError as exc:
        error = f'{exc} {CHOOSE_FILES_AGAIN}' if files else str(exc)
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view',
            comment_text=text, comment_error=error, comment_mentions=mentions,
        )
    return redirect(_card_url(board, card, request, tab='chat'))


# --------------------------------------------------------------------------
# Files of «Чат»: protected media
# --------------------------------------------------------------------------
#
# A file of a card's chat is served only here, after reading the board is
# asked again; the row is found through the card in the address, so a valid
# id of another card is a 404, and a refusal, a deleted file and a missing
# one are all the same 404. `MEDIA_ROOT` is never published.

def _card_file_or_404(request, pk, card_pk, file_pk, operation):
    board = _board_or_404(pk)
    card_file = BoardCardFile.objects.select_related('card').filter(
        pk=file_pk, card_id=card_pk, card__board=board,
    ).first()
    if card_file is None or not can_view_board(request.user, board):
        if card_file is not None:
            # Identifiers only — never the file's name, path or type.
            log_event(
                file_logger, 'WARNING', 'board.file_access_denied',
                board_id=board.pk, board_card_id=card_pk, board_card_file_id=file_pk,
                user_id=getattr(request.user, 'pk', None), operation=operation, outcome='denied',
            )
        raise Http404('Файл не найден.')
    if card_file.deleted_at is not None or not card_file.file:
        raise Http404('Файл не найден.')
    card_file.card.board = board
    return board, card_file


def _open_card_file(request, board, card_file, operation):
    try:
        handle = card_file.file.open('rb')
    except OSError as exc:
        log_event(
            file_logger, 'ERROR', 'board.file_storage_failed',
            board_id=board.pk, board_card_id=card_file.card_id, board_card_file_id=card_file.pk,
            user_id=request.user.pk, operation=operation, error_type=type(exc).__name__,
            outcome='failed',
        )
        raise Http404('Файл не найден.') from exc
    log_event(
        file_logger, 'INFO', 'board.file_downloaded',
        board_id=board.pk, board_card_id=card_file.card_id, board_card_file_id=card_file.pk,
        user_id=request.user.pk, size_bytes=card_file.size, operation=operation, outcome='ok',
    )
    return handle


@login_required
@require_GET
def file_download(request, pk, card_pk, file_pk):
    """A file of «Чат», as a download — whoever reads the board."""
    board, card_file = _card_file_or_404(request, pk, card_pk, file_pk, 'download')
    handle = _open_card_file(request, board, card_file, 'download')
    return FileResponse(
        handle,
        as_attachment=True,
        filename=card_file.original_name,
        content_type=card_file.content_type or 'application/octet-stream',
    )


@login_required
@require_GET
def file_preview(request, pk, card_pk, file_pk):
    """An image of «Чат», inline — the thumbnail and «open in a new tab».

    Only `.png`, `.jpg`, `.jpeg` and `.webp` (`PREVIEW_IMAGE_TYPES`), and the
    type served is the extension's, never the stored `content_type` the
    browser sent: anything else is a 404. `nosniff` and a sandboxing CSP keep
    the response an image whatever its bytes are.
    """
    board, card_file = _card_file_or_404(request, pk, card_pk, file_pk, 'preview')
    content_type = PREVIEW_IMAGE_TYPES.get(card_file.extension)
    if content_type is None:
        raise Http404('Предпросмотра у этого файла нет.')
    handle = _open_card_file(request, board, card_file, 'preview')
    response = FileResponse(handle, content_type=content_type)
    response['Content-Disposition'] = 'inline'
    response['X-Content-Type-Options'] = 'nosniff'
    response['Content-Security-Policy'] = 'sandbox'
    response['Cache-Control'] = 'private, max-age=300'
    return response


@login_required
def file_delete(request, pk, card_pk, file_pk):
    """«×» beside a file of «Чат»: `delete_card_file()`.

    Asked before the method: reading the board (a 404 otherwise, as for a
    download) and `can_delete_card_file()` (a 403). A GET changes nothing and
    goes back to the chat; a refusal of the service comes back as a message.
    `?chat=files` (the «×» of «Только файлы») returns there.
    """
    board = _board_or_404(pk)
    card_file = get_object_or_404(
        BoardCardFile.objects.select_related('card'), pk=file_pk, card_id=card_pk, card__board=board,
    )
    if not can_view_board(request.user, board):
        raise Http404('Файл не найден.')
    card = card_file.card
    card.board = board
    tab = 'files' if request.GET.get('chat') == CHAT_FILES else 'chat'
    if request.method != 'POST':
        return redirect(_card_url(board, card, request, tab=tab))
    if card_file.deleted_at is None:
        _require(can_delete_card_file(request.user, card_file, board))
    try:
        deleted = delete_card_file(card_file, actor=request.user)
    except BoardError as exc:
        messages.error(request, str(exc))
    else:
        if deleted:
            messages.success(request, 'Файл удалён.')
    return redirect(_card_url(board, card, request, tab=tab))


@login_required
def card_subscribe(request, pk, card_pk):
    """«Следить» / «Вы следите» in the card panel's heading:
    `toggle_card_subscription()`.

    Reading the board is asked before the method; the service refuses an
    archived board. The form posts the state it asks for (`subscribe` 1 or
    0) and the tab it was on, which the redirect opens again.
    """
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    tab = parse_panel_tab(request.POST.get('tab') or request.GET.get('tab'))
    if request.method != 'POST':
        return redirect(_card_url(board, card, request, tab=tab))
    wanted = request.POST.get('subscribe')
    try:
        following = toggle_card_subscription(
            card, actor=request.user, subscribe=None if wanted not in ('0', '1') else wanted == '1',
        )
    except BoardError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            f'Вы следите за карточкой {card.code}: сообщения, отмена и выполнение придут в колокольчик.'
            if following else f'Вы больше не следите за карточкой {card.code}.',
        )
    return redirect(_card_url(board, card, request, tab=tab))


# --------------------------------------------------------------------------
# «Чек-лист»
# --------------------------------------------------------------------------
#
# Ordinary POST forms on «Описание», so the list works without JavaScript; the
# right — working on the board — is asked before the method, and every refusal
# of the service (a closed card, an archived board, an empty or a long text,
# the limit) comes back on the card. A tick sent by `board_checklist.js`
# (`X-Requested-With: fetch`) is answered in JSON instead, like a drag.


def _checklist_request(request, pk, card_pk, item_pk=None, *, board=None):
    """The board, the card and the item, the right asked before the method."""
    board = board or _board_or_404(pk)
    _require(can_work_on_board(request.user, board))
    card = get_object_or_404(BoardCard.objects.select_related('board'), pk=card_pk, board=board)
    item = (
        get_object_or_404(BoardCardChecklistItem, pk=item_pk, card=card)
        if item_pk is not None else None
    )
    return board, card, item


def _checklist_back(request, board, card):
    return redirect(_card_url(board, card, request, tab='description'))


@login_required
def checklist_add(request, pk, card_pk):
    """«Добавить пункт»: `add_checklist_item()`; a refusal keeps the text."""
    board, card, _item = _checklist_request(request, pk, card_pk)
    if request.method != 'POST':
        return _checklist_back(request, board, card)
    text = request.POST.get('text', '')
    try:
        add_checklist_item(card, actor=request.user, text=text)
    except BoardError as exc:
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view',
            checklist_text=text, checklist_error=str(exc),
        )
    return _checklist_back(request, board, card)


@login_required
def checklist_rename(request, pk, card_pk, item_pk):
    """«Изменить» → «Сохранить»: `rename_checklist_item()`; a refusal
    keeps the form open with what was typed."""
    board, card, item = _checklist_request(request, pk, card_pk, item_pk)
    if request.method != 'POST':
        return _checklist_back(request, board, card)
    text = request.POST.get('text', '')
    try:
        rename_checklist_item(item, actor=request.user, text=text)
    except BoardError as exc:
        return _render_board(
            request, board, card.sub_board, card_id=card.pk, panel='view', edit_item=item.pk,
            checklist_edit_text=text, checklist_edit_error=str(exc),
        )
    return _checklist_back(request, board, card)


@login_required
def checklist_toggle(request, pk, card_pk, item_pk):
    """The checkbox of an item: `toggle_checklist_item()` with the state
    the form asks for (`done` 1 or 0).

    From `board_checklist.js` (`X-Requested-With: fetch`) the answer is JSON
    — `{"ok": true, "item_id", "is_done", "done", "total"}`, or
    `{"ok": false, "error"}` with 400 (the service refused) or 403 (no right,
    asked before the method) — identifiers and counts only. Otherwise the
    card again, a refusal as a message.
    """
    fetch = _is_fetch(request)
    board = _board_or_404(pk)
    if fetch and not can_work_on_board(request.user, board):
        return JsonResponse(
            {'ok': False, 'error': 'Работа с карточками этой доски недоступна.'}, status=403,
        )
    board, card, item = _checklist_request(request, pk, card_pk, item_pk, board=board)
    if request.method != 'POST':
        return _checklist_back(request, board, card)
    wanted = request.POST.get('done')
    try:
        item = toggle_checklist_item(
            item, actor=request.user, done=None if wanted not in ('0', '1') else wanted == '1',
        )
    except BoardError as exc:
        if fetch:
            return JsonResponse({'ok': False, 'error': str(exc)}, status=400)
        messages.error(request, str(exc))
        return _checklist_back(request, board, card)
    if fetch:
        done, total = checklist_counts(card)
        return JsonResponse({
            'ok': True, 'item_id': item.pk, 'is_done': item.is_done, 'done': done, 'total': total,
        })
    return _checklist_back(request, board, card)


@login_required
def checklist_to_subtask(request, pk, card_pk, item_pk):
    """«В подзадачу» of an item, confirmed through the shared modal:
    `checklist_item_to_subtask()`; the card stays on «Описание»."""
    board, card, item = _checklist_request(request, pk, card_pk, item_pk)
    if request.method == 'POST':
        try:
            subtask = checklist_item_to_subtask(item, actor=request.user)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f'Пункт стал подзадачей {subtask.code}.')
    return _checklist_back(request, board, card)


@login_required
def checklist_move(request, pk, card_pk, item_pk):
    """↑ ↓ of an item: `move_checklist_item()`."""
    board, card, item = _checklist_request(request, pk, card_pk, item_pk)
    if request.method == 'POST':
        try:
            move_checklist_item(item, actor=request.user, direction=request.POST.get('direction'))
        except BoardError as exc:
            messages.error(request, str(exc))
    return _checklist_back(request, board, card)


@login_required
def checklist_delete(request, pk, card_pk, item_pk):
    """«×» of an item, confirmed through the shared modal:
    `delete_checklist_item()`."""
    board, card, item = _checklist_request(request, pk, card_pk, item_pk)
    if request.method == 'POST':
        try:
            delete_checklist_item(item, actor=request.user)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, 'Пункт чек-листа удалён.')
    return _checklist_back(request, board, card)


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


@login_required
def board_change_code(request, pk):
    """«Код доски» in the board's «⋯» menu: `change_board_code()`."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method == 'POST':
        form = BoardCodeForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                change_board_code(board, actor=request.user, code=form.cleaned_data['code'])
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
    query = _request_filters(request, board).query if request is not None else ''
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
def column_pins(request, pk, sub_pk, column_pk):
    """«Закреплённые исполнители» in a column's «⋯» menu: `set_column_pins()`.

    The manager's right, asked before the method like every structure route;
    a refusal (somebody not on the board, the closing column, an archived
    board) is a message on the sub-board.
    """
    board, sub_board = _structure_request(request, pk, sub_pk)
    column = _column_or_404(sub_board, column_pk)
    if request.method == 'POST':
        form = ColumnPinsForm(request.POST)
        if not form.is_valid():
            _refused(request, form)
        else:
            try:
                set_column_pins(
                    column, actor=request.user,
                    user_ids=form.cleaned_data['users'], mode=form.cleaned_data['mode'],
                )
            except BoardError as exc:
                _refused(request, str(exc))
            else:
                messages.success(request, f'Закреплённые исполнители колонки «{column.name}» сохранены.')
    return _back(board, sub_board.pk, request)


@login_required
def column_stale(request, pk, sub_pk, column_pk):
    """«Застой» in a column's «⋯» menu: `set_column_stale_days()`.

    The manager's right, asked before the method like every structure route;
    an empty number switches it off. A refusal (out of 1–365, the closing
    column, an archived board) is a message on the sub-board.
    """
    board, sub_board = _structure_request(request, pk, sub_pk)
    column = _column_or_404(sub_board, column_pk)
    if request.method == 'POST':
        form = ColumnStaleForm(request.POST)
        if not form.is_valid():
            _refused(request, 'Застой задаётся целым числом дней или не задаётся вовсе.')
        else:
            try:
                updated = set_column_stale_days(column, actor=request.user, days=form.cleaned_data['days'])
            except BoardError as exc:
                _refused(request, str(exc))
            else:
                messages.success(
                    request,
                    f'Колонка «{column.name}»: подсвечивать карточки через {updated.stale_after_days} дн.'
                    if updated.stale_after_days else f'Колонка «{column.name}»: подсветка застоя выключена.',
                )
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


# --------------------------------------------------------------------------
# «Поля карточек»
# --------------------------------------------------------------------------
#
# `/work/boards/<board>/fields/`: the board's own card fields, in order, with
# their options. Every reader of the board reads the page; whoever manages it
# (`can_manage_board()`: the owner or an administrator, never an archived
# board) also gets the forms. Every route below asks that right before the
# method (a 403), answers a GET by going back to the page and changing nothing,
# and comes back with a message: the service's refusal or what was done.


def _fields_url(board, anchor=''):
    url = reverse('boards:fields', args=[board.pk])
    return f'{url}#{anchor}' if anchor else url


def _render_fields(request, board, *, form=None, option_rows=None):
    """The page: the fields with how many cards hold a value of each (and of
    each option), and for a manager the forms. Three queries for the list,
    whatever its length."""
    from django.db.models import Count, Prefetch

    can_manage = can_manage_board(request.user, board)
    fields = list(
        BoardField.objects.filter(board=board).order_by('position', 'pk')
        .annotate(value_count=Count('values'))
        .prefetch_related(Prefetch(
            'options',
            queryset=BoardFieldOption.objects.order_by('position', 'pk').annotate(value_count=Count('values')),
        ))
    )
    rows = []
    for index, field in enumerate(fields):
        options = list(field.options.all())
        rows.append({
            'field': field,
            'options': [
                {
                    'option': option,
                    'can_move_left': position > 0,
                    'can_move_right': position < len(options) - 1,
                }
                for position, option in enumerate(options)
            ],
            'live_options': sum(not option.is_archived for option in options),
            'can_move_left': index > 0,
            'can_move_right': index < len(fields) - 1,
        })
    if form is None:
        form = FieldForm()
    if option_rows is None:
        option_rows = form.option_rows or [{'label': '', 'color': BoardFieldColor.GRAY}]
    live = sum(not field.is_archived for field in fields)
    return render(request, 'boards/fields.html', {
        **_frame(request, board),
        'header_title': f'Поля карточек · {board.name}',
        'board': board,
        'rows': rows,
        'can_manage': can_manage,
        'form': form,
        'option_rows': option_rows,
        'kinds': BoardField.Kind.choices,
        'colors': BoardFieldColor.choices,
        'live_count': live,
        'can_add_field': live < MAX_FIELDS,
        'max_fields': MAX_FIELDS,
        'max_options': MAX_OPTIONS,
    })


@login_required
def board_fields_page(request, pk):
    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    return _render_fields(request, board)


@login_required
def field_create(request, pk):
    """«+ Поле»: `create_field()`. «+ Вариант» without JavaScript posts the
    same form back with one more option row, writing nothing; a refusal
    comes back as the page with what was typed and the message."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    if request.method != 'POST':
        return redirect(_fields_url(board))
    form = FieldForm(request.POST)
    if 'add_option_row' in request.POST:
        rows = form.option_rows + [{'label': '', 'color': BoardFieldColor.GRAY}]
        return _render_fields(
            request, board,
            form=FieldForm(initial={'name': request.POST.get('name', ''), 'kind': request.POST.get('kind', '')}),
            option_rows=rows,
        )
    if form.is_valid():
        try:
            field = create_field(
                board, actor=request.user,
                name=form.cleaned_data['name'], kind=form.cleaned_data['kind'],
                options=form.options(),
            )
        except BoardError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, f'Поле «{field.name}» добавлено.')
            return redirect(_fields_url(board, f'field-{field.pk}'))
    return _render_fields(request, board, form=form)


def _field_route(request, pk, field_pk, action, *, success='', gone=False):
    """The shape of every route on one field: the right before the method, a
    GET back to the page, a refusal as a message. Back at the field's own
    row — or at the top once `gone` (deleted)."""
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    field = get_object_or_404(BoardField, pk=field_pk, board=board)
    anchor = f'field-{field.pk}'
    if request.method == 'POST':
        try:
            action(field)
        except BoardError as exc:
            messages.error(request, str(exc))
        else:
            if success:
                messages.success(request, success.format(name=field.name))
            if gone:
                anchor = ''
    return redirect(_fields_url(board, anchor))


@login_required
def field_update(request, pk, field_pk):
    def action(field):
        form = FieldUpdateForm(request.POST)
        if not form.is_valid():
            raise BoardError(' '.join(error for errors in form.errors.values() for error in errors))
        update_field(
            field, actor=request.user, name=form.cleaned_data['name'],
            kind=form.cleaned_data['kind'] or None, show_on_tile=form.cleaned_data['show_on_tile'],
        )
    return _field_route(request, pk, field_pk, action)


@login_required
def field_move(request, pk, field_pk):
    def action(field):
        form = DirectionForm(request.POST)
        if not form.is_valid():
            raise BoardError('Неизвестное направление.')
        move_field(field, actor=request.user, direction=form.cleaned_data['direction'])
    return _field_route(request, pk, field_pk, action)


@login_required
def field_archive(request, pk, field_pk):
    return _field_route(
        request, pk, field_pk, lambda field: archive_field(field, actor=request.user),
        success='Поле «{name}» убрано в архив.',
    )


@login_required
def field_restore(request, pk, field_pk):
    return _field_route(
        request, pk, field_pk, lambda field: restore_field(field, actor=request.user),
        success='Поле «{name}» возвращено.',
    )


@login_required
def field_delete(request, pk, field_pk):
    return _field_route(
        request, pk, field_pk, lambda field: delete_field(field, actor=request.user),
        success='Поле «{name}» удалено.', gone=True,
    )


@login_required
def option_create(request, pk, field_pk):
    def action(field):
        form = OptionForm(request.POST)
        if not form.is_valid():
            raise BoardError(' '.join(error for errors in form.errors.values() for error in errors))
        create_option(field, actor=request.user, label=form.cleaned_data['label'], color=form.cleaned_data['color'])
    return _field_route(request, pk, field_pk, action)


def _option_route(request, pk, field_pk, option_pk, action):
    board = _board_or_404(pk)
    _require(can_manage_board(request.user, board))
    field = get_object_or_404(BoardField, pk=field_pk, board=board)
    option = get_object_or_404(BoardFieldOption, pk=option_pk, field=field)
    if request.method == 'POST':
        try:
            action(option)
        except BoardError as exc:
            messages.error(request, str(exc))
    return redirect(_fields_url(board, f'field-{field.pk}'))


@login_required
def option_update(request, pk, field_pk, option_pk):
    def action(option):
        form = OptionForm(request.POST)
        if not form.is_valid():
            raise BoardError(' '.join(error for errors in form.errors.values() for error in errors))
        update_option(option, actor=request.user, label=form.cleaned_data['label'], color=form.cleaned_data['color'])
    return _option_route(request, pk, field_pk, option_pk, action)


@login_required
def option_move(request, pk, field_pk, option_pk):
    def action(option):
        form = DirectionForm(request.POST)
        if not form.is_valid():
            raise BoardError('Неизвестное направление.')
        move_option(option, actor=request.user, direction=form.cleaned_data['direction'])
    return _option_route(request, pk, field_pk, option_pk, action)


@login_required
def option_archive(request, pk, field_pk, option_pk):
    return _option_route(
        request, pk, field_pk, option_pk, lambda option: archive_option(option, actor=request.user),
    )


@login_required
def option_restore(request, pk, field_pk, option_pk):
    return _option_route(
        request, pk, field_pk, option_pk, lambda option: restore_option(option, actor=request.user),
    )


@login_required
def option_delete(request, pk, field_pk, option_pk):
    return _option_route(
        request, pk, field_pk, option_pk, lambda option: delete_option(option, actor=request.user),
    )


# --------------------------------------------------------------------------
# «Отклонения»
# --------------------------------------------------------------------------
#
# `/work/boards/<board>/deviations/`: why the board's deadlines moved — a
# summary per reason and the moves themselves, for a period (`from`/`to`,
# the last 30 days by default) and one sub-board or all (`sub`). Read by every
# reader of the board; nothing here writes. `&export=xlsx` is the list as a
# spreadsheet — the very rows the page shows.


def _report_date(value, default):
    """An ISO date of the address, or `default` for anything else."""
    import datetime

    try:
        return datetime.date.fromisoformat((value or '').strip())
    except ValueError:
        return default


@login_required
def board_deviations(request, pk):
    import datetime

    board = _board_or_404(pk)
    _require(can_view_board(request.user, board))
    today = timezone.localdate()
    date_to = _report_date(request.GET.get('to'), today)
    date_from = _report_date(request.GET.get('from'), date_to - datetime.timedelta(days=DEVIATION_DEFAULT_DAYS - 1))
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    tabs = board_tabs(board)
    sub_pk = request.GET.get('sub', '')
    sub_board = _tab_of(tabs, int(sub_pk)) if sub_pk.isdigit() else None
    report = build_deviation_report(board, date_from=date_from, date_to=date_to, sub_board=sub_board)
    query = urlencode([
        ('from', date_from.isoformat()), ('to', date_to.isoformat()),
        *([('sub', sub_board.pk)] if sub_board is not None else []),
    ])
    if request.GET.get('export') == 'xlsx':
        stem = '-'.join(part for part in (
            safe_file_part(board.code), 'otkloneniya', safe_file_part(sub_board.name) if sub_board else '',
        ) if part)
        return xlsx_response(
            stem,
            f'{board.code} Отклонения',
            ['Дата', 'Карточка', 'Название', 'Поддоска', 'Было', 'Стало', 'Сдвиг, дн.', 'Причина',
             'Комментарий', 'Кто'],
            [
                [
                    row['at'].date(), row['card'].code, row['card'].title,
                    next((tab.name for tab in tabs if tab.pk == row['card'].sub_board_id), None),
                    row['old_due'], row['new_due'], row['shift'], row['reason'].name,
                    row['comment'] or None, person_name(row['who']),
                ]
                for row in report['rows']
            ],
            stamp_separator='-',
            typed_dates=True,
        )
    return render(request, 'boards/deviations.html', {
        **_frame(request, board),
        **report,
        'header_title': f'Отклонения · {board.name}',
        'board': board,
        'tabs': tabs,
        'sub_board': sub_board,
        'date_from': date_from,
        'date_to': date_to,
        'export_url': f"{reverse('boards:deviations', args=[board.pk])}?{query}&export=xlsx",
        'moves_label': f"{report['moves']} {plural_ru(report['moves'], 'перенос', 'переноса', 'переносов')}",
    })

