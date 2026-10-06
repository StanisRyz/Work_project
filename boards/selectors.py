"""What the board pages show: the left panel, one sub-board's columns, one card.

Read only, and never a permission decision of its own — the flags returned are
`boards.permissions` asked once. The number of queries depends neither on the
number of cards nor on the number of columns: the tabs and the columns are one
query each, the open cards and the latest completed ones are each one query
through their tasks, the исполнители one prefetch each, and the card the panel
shows one more.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

import re

from django.contrib.auth import get_user_model
from django.db.models import Count, IntegerField, OuterRef, Prefetch, Q, Subquery, Value
from django.db.models import prefetch_related_objects
from django.db.models.functions import Coalesce
from django.utils import timezone

from .columns import MAX_COLUMNS, card_column
from .models import Board, BoardCardComment, BoardCardEvent, BoardColumn, BoardMember, SubBoard
from .permissions import (
    active_employee_q,
    can_cancel_card,
    can_manage_board,
    readable_boards_q,
    can_restore_board,
    can_work_on_board,
)


# How many completed cards the closing column draws. The rest are counted, not read:
# a board in use for a year has hundreds of them and nobody scrolls that far.
DONE_LIMIT = 50

# How many messages of «Обсуждение» the panel draws: the newest ones, oldest
# first. The earlier ones are counted, not read, and `?comments=all` shows
# them — a card discussed for months stays one query and a short list.
COMMENTS_LIMIT = 100

# How many member avatars the board heading draws before «+N».
MEMBER_PREVIEW_LIMIT = 5

# How many pinned people a column header draws before «+N».
PIN_PREVIEW_LIMIT = 3

# The longest `?q=` a board search reads; anything past it is dropped.
SEARCH_MAX_LENGTH = 200


@dataclass(frozen=True)
class BoardFilters:
    """What the board shows: `?mine=1`, `?overdue=1`, `?q=<text>`.

    Parsed once by `parse_board_filters()` for the page, its fragment and the
    drag's JSON counts alike, so the three can never filter differently.
    """

    mine: bool = False
    overdue: bool = False
    q: str = ''

    @property
    def is_active(self):
        return self.mine or self.overdue or bool(self.q)

    @property
    def query(self):
        """The filter as a query string without `?` — `''` when none is set."""
        params = []
        if self.mine:
            params.append(('mine', '1'))
        if self.overdue:
            params.append(('overdue', '1'))
        if self.q:
            params.append(('q', self.q))
        return urlencode(params)


NO_FILTERS = BoardFilters()


def parse_board_filters(params):
    """`BoardFilters` from a request's GET (or any mapping of strings)."""
    return BoardFilters(
        mine=params.get('mine') == '1',
        overdue=params.get('overdue') == '1',
        q=(params.get('q') or '').strip()[:SEARCH_MAX_LENGTH],
    )


# A bare card number in the search: «12», «№12», «#12».
_CARD_NUMBER = re.compile(r'^\s*[№#]?\s*(\d+)\s*$')


def card_search_q(q):
    """The board search over a sub-board's tasks: the title, or the card's code.

    A substring of the title; «ZAP-12» in any case (the board's code and the
    number — `tasks.selectors.board_card_code_filter()`, the registry's own
    rule); or a bare number («12», «№12»). The tasks searched are already the
    board's own, so a code of another board finds nothing here.
    """
    from tasks.selectors import board_card_code_filter

    condition = Q(board_card__title__icontains=q)
    card_code = board_card_code_filter(q)
    if card_code is not None:
        condition |= card_code
    number = _CARD_NUMBER.match(q)
    if number is not None:
        condition |= Q(board_card__number=int(number.group(1)))
    return condition


def _filtered(tasks, filters, user, *, open_work):
    """`tasks` narrowed by `filters`; `overdue` only ever narrows open work.

    «Мои» is «I am an исполнитель» (`TaskAssignee`, one row per person, so the
    join adds no duplicates); `q` is a substring of the card's title or its
    code (`card_search_q()`).
    """
    if filters.mine:
        tasks = tasks.filter(assignees__user=user)
    if filters.q:
        tasks = tasks.filter(card_search_q(filters.q))
    if filters.overdue and open_work:
        tasks = tasks.filter(due_date__lt=timezone.localdate())
    return tasks


def _board_tasks(sub_board):
    """The sub-board's tasks with their cards, исполнители and message counts.

    The «Обсуждение» count is a subquery annotation of the same query, so a
    tile's counter costs no query of its own.
    """
    return _tasks_with_cards(
        _all_board_tasks().filter(board_card__sub_board=sub_board)
    )


def _all_board_tasks():
    from tasks.models import Task

    return Task.objects.filter(source_type=Task.SourceType.BOARD)


def _tasks_with_cards(tasks):
    """`tasks` with what a tile reads: status, card, исполнители, messages.

    The исполнители are one prefetch query with their accounts joined — a
    tile draws a name and initials, nothing of the profile.
    """
    from tasks.models import TaskAssignee

    comments = BoardCardComment.objects.filter(card=OuterRef('board_card'))
    return (
        tasks.select_related('status', 'board_card')
        .prefetch_related(Prefetch('assignees', queryset=TaskAssignee.objects.select_related('user')))
        .annotate(comment_count=_count_subquery(comments, 'card'))
    )


def _item(task, board):
    """One card as every board template reads it.

    `board` is the page's own board, attached to the card so its code
    («ZAP-12», `BoardCard.code`) costs no query per tile.
    """
    card = task.board_card
    card.board = board
    return {
        'card': card,
        'task': task,
        'assignees': [assignee.user for assignee in task.assignees.all()],
        'due_date': task.due_date,
        'is_closed': task.status.is_final,
        'comment_count': getattr(task, 'comment_count', 0),
    }


def _panel_card(board, sub_board, columns_of, card_id, user, *, loaded, tabs, can_work,
                all_comments=False):
    """The card `?card=` names, with its task — or `None`.

    Any card of this board: one of this sub-board, or one moved to another
    sub-board while this page had it open — then `moved_to` is that sub-board
    and the panel says where the card is now, with a link, without going
    there itself. `None` for anything else: a card of another board, a
    missing id, text. Unlike the columns, a cancelled card is found too — its
    panel is the read-only record of what was withdrawn.

    The panel is where a `BOARD` task is worked, so the card also carries what
    the task page used to show: its attachments
    (`tasks.presentation.task_attachment_cards()`, the task page's own list)
    and the task rights, each asked once of `tasks.permissions` — the board
    has no rules of its own about completing, reopening or files.

    `loaded` are the tasks the columns already read (`{card id: task}`): the
    open card is almost always among them, and its исполнители are taken from
    there instead of being read twice. `columns_of` is the board's columns by
    sub-board, `tabs` its sub-boards — both already read for the page.
    `can_work` is `can_work_on_board()`, asked once for the page: writing in
    «Чат» is exactly that right (`can_comment_card()`).
    """
    from tasks.models import TaskAssignee
    from tasks.permissions import (
        can_complete_task,
        can_reopen_task,
        can_upload_task_attachment,
    )
    from tasks.presentation import task_attachment_cards

    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return None
    comments = BoardCardComment.objects.filter(card=OuterRef('board_card'))
    task = (
        _all_board_tasks()
        .filter(board_card__board=board, board_card_id=card_id)
        .select_related(
            'status', 'board_card', 'board_card__created_by', 'department',
            'completed_by', 'cancelled_by',
        )
        .annotate(comment_count=_count_subquery(comments, 'card'))
        .first()
    )
    if task is None:
        return None
    known = loaded.get(card_id)
    if known is not None:
        task._prefetched_objects_cache = dict(known._prefetched_objects_cache)
    else:
        prefetch_related_objects(
            [task], Prefetch('assignees', queryset=TaskAssignee.objects.select_related('user')),
        )
    item = _item(task, board)
    card = task.board_card
    own_columns = columns_of.get(card.sub_board_id, [])
    item['column'] = card_column(card, task, own_columns)
    # Where «Вернуть в работу» puts it: its working column, or the first one
    # if that column was deleted meanwhile.
    item['return_column'] = _return_column(card, own_columns)
    item['moved_to'] = (
        next((tab for tab in tabs if tab.pk == card.sub_board_id), None)
        if card.sub_board_id != sub_board.pk else None
    )
    card.sub_board = item['moved_to'] or sub_board
    item['department'] = task.department
    item['attachments'] = task_attachment_cards(task, user)
    item['can_complete'] = can_complete_task(task, user)
    # An archived board stays as it was shelved: `reopen_card()` refuses it.
    item['can_reopen'] = can_reopen_task(task, user) and not board.is_archived
    item['can_upload_attachment'] = can_upload_task_attachment(task, user)
    item['can_cancel'] = (
        task.status.code == 'IN_PROGRESS' and can_cancel_card(user, card)
    )
    # «Обсуждение»: the newest `COMMENTS_LIMIT` messages, oldest first, in one
    # query sliced by the database — or every one under `all_comments`. How
    # many are left out is the card's `comment_count`, already annotated, so
    # there is no second query. Writing is `can_comment_card()` — i.e.
    # `can_work_on_board()`, already asked for the page — whatever the state
    # of the task.
    messages = BoardCardComment.objects.filter(card=card).select_related('author')
    if all_comments:
        item['comments'] = list(messages)
    else:
        item['comments'] = list(messages.order_by('-created_at', '-pk')[:COMMENTS_LIMIT])[::-1]
    item['comments_earlier'] = max(item['comment_count'] - len(item['comments']), 0)
    item['can_comment'] = can_work
    item['log'] = card_log(card, item['attachments'])
    return item


# The fields an «Изменение» entry names, in the edit form's order.
EDITED_FIELD_LABELS = {
    'title': 'заголовок',
    'description': 'описание',
    'due_date': 'срок',
    'assignees': 'исполнители',
}


def _place(details, prefix):
    """«Сделать», or «Основная / Сделать» when the entry names the sub-board."""
    column = details.get(f'{prefix}_column') or '—'
    sub_board = details.get(f'{prefix}_sub_board')
    return f'{sub_board} / {column}' if sub_board else column


def describe_card_event(event, code=''):
    """The sentence «Лог» shows for one `BoardCardEvent`.

    Built from the kind and the stored snapshots only — a column's or a
    sub-board's name is the one it had then — and neutral in person:
    «Перенос: «Сделать» → «В работе»», never a verb that would need the
    actor's gender. `code` is the card's («ZAP-12»), named by the entry of
    its creation.
    """
    details = event.details if isinstance(event.details, dict) else {}
    kind = event.kind
    if kind == BoardCardEvent.Kind.CREATED:
        column = details.get('column')
        created = f'Карточка {code} создана' if code else 'Карточка создана'
        return f'{created} в колонке «{column}»' if column else created
    if kind == BoardCardEvent.Kind.EDITED:
        if details.get('by_column'):
            # The people pinned to the column the card came into.
            return f'Исполнители по колонке «{details["by_column"]}»'
        names = [
            EDITED_FIELD_LABELS[name]
            for name in details.get('fields') or () if name in EDITED_FIELD_LABELS
        ]
        return f'Изменено: {", ".join(names)}' if names else 'Карточка изменена'
    if kind == BoardCardEvent.Kind.MOVED:
        return f'Перенос: «{_place(details, "from")}» → «{_place(details, "to")}»'
    if kind == BoardCardEvent.Kind.COMPLETED:
        return 'Задача выполнена'
    if kind == BoardCardEvent.Kind.REOPENED:
        column = details.get('column')
        return f'Возвращена в работу, в колонку «{column}»' if column else 'Возвращена в работу'
    if kind == BoardCardEvent.Kind.CANCELLED:
        return 'Карточка отменена'
    return event.get_kind_display()


def card_log(card, attachments):
    """«Лог» of a card, newest first: its journal and its files.

    The journal is one query (`BoardCardEvent`, its authors joined); the files
    are the panel's own attachment list, already read, so a file costs no
    query here — they are not journal entries, only shown beside them, by
    time: who added which file and when.
    """
    entries = [
        {
            'kind': event.kind.lower(),
            'actor': event.actor,
            'at': event.created_at,
            'text': describe_card_event(event, card.code),
            'order': (event.created_at, 1, event.pk),
        }
        for event in BoardCardEvent.objects.filter(card=card).select_related('actor')
    ]
    entries.extend(
        {
            'kind': 'file',
            'actor': item['object'].uploaded_by,
            'at': item['object'].created_at,
            'text': f'Добавлен файл «{item["object"].original_name}»',
            'order': (item['object'].created_at, 0, item['object'].pk),
        }
        for item in attachments
    )
    entries.sort(key=lambda entry: entry['order'], reverse=True)
    return entries


def _return_column(card, columns):
    working = [column for column in columns if not column.is_done]
    return next(
        (column for column in working if column.pk == card.column_id),
        working[0] if working else None,
    )


def sub_board_columns(sub_board):
    """The sub-board's columns, in order — one query."""
    return list(BoardColumn.objects.filter(sub_board=sub_board).order_by('position', 'pk'))


def first_sub_board(board):
    """The board's first tab — where `/work/boards/<board>/` leads."""
    return SubBoard.objects.filter(board=board).order_by('position', 'pk').first()


def _move_choices(tabs, columns_of, current):
    """«Переместить в…»: every working column of the board, by sub-board.

    `<optgroup>`s in the select — the current sub-board first, then the
    others in tab order — as Django's grouped choices.
    """
    ordered = [current] + [tab for tab in tabs if tab.pk != current.pk]
    return [
        (tab.name, [(column.pk, column.name) for column in columns_of.get(tab.pk, []) if not column.is_done])
        for tab in ordered
        if any(not column.is_done for column in columns_of.get(tab.pk, []))
    ]


def build_board_state(board, sub_board, user, *, done_limit=DONE_LIMIT, card_id=None,
                      filters=NO_FILTERS, all_comments=False):
    """Everything one sub-board page renders.

    `sub_boards` are the board's tabs, each `{'sub_board', 'is_active',
    'can_move_left', 'can_move_right'}`. `columns` are this sub-board's own,
    left to right; each is `{'column', 'pk', 'name', 'is_done', 'cards',
    'count', 'more', 'can_move_left', 'can_move_right', 'can_delete',
    'pinned', 'pinned_more', 'pinned_ids'}`, and each card `{'card', 'task',
    'assignees', 'due_date', 'is_closed'}`. The working columns are in
    `position` order. The closing column holds the `done_limit` newest
    completions and `more` counts the rest. A card whose task was cancelled
    is on none of them. The `can_*` flags of a tab or a column say only
    which of ← → «Удалить» to draw for a manager; the services decide.
    `pinned` are the first `PIN_PREVIEW_LIMIT` people pinned to a column
    (its header's avatars), `pinned_more` the rest; `members` — for whoever
    manages the board — the active members its pin menus offer.

    `card` is the card `card_id` names (see `_panel_card()`), else `None` —
    found whatever the filters say, so the open panel never disappears. Its
    «Обсуждение» holds the newest `COMMENTS_LIMIT` messages and counts the
    rest in `comments_earlier`; `all_comments` (`?comments=all`) reads them all.
    `move_choices` are the working columns of every sub-board, grouped, for
    its «Переместить в…».

    `filters` (`BoardFilters`) narrows the columns and their counts: «Мои»
    and the search apply to every column, «Просроченные» to the open ones
    only — a completed card is never overdue.

    Each open card also says what this user may do with it by dragging —
    markup only, the routes ask again: `is_movable` (may work on the board)
    and `can_complete` (`tasks.permissions.can_complete_task()`, asked for the
    whole sub-board in one query through `completable_task_ids()`).

    The board's columns are one query for all its sub-boards — the panel's
    «Переместить в…» offers them all — and the pins of this sub-board's
    columns one more.
    """
    from tasks.permissions import completable_task_ids

    can_work = can_work_on_board(user, board)
    can_manage = can_manage_board(user, board)
    tabs = list(SubBoard.objects.filter(board=board).order_by('position', 'pk'))
    columns_of = {}
    for column in BoardColumn.objects.filter(sub_board__board=board).order_by('position', 'pk'):
        columns_of.setdefault(column.sub_board_id, []).append(column)
    columns = columns_of.get(sub_board.pk, [])
    prefetch_related_objects(columns, Prefetch(
        'pinned_assignees',
        queryset=get_user_model().objects.order_by('last_name', 'first_name', 'username', 'pk'),
    ))
    tasks = _board_tasks(sub_board)
    open_tasks = list(_filtered(tasks.filter(status__is_final=False), filters, user, open_work=True))
    done_tasks = _filtered(tasks.filter(status__code='COMPLETED'), filters, user, open_work=False)
    completable = completable_task_ids([task.pk for task in open_tasks], user)
    cards_by_column = {column.pk: [] for column in columns}
    for task in open_tasks:
        column = card_column(task.board_card, task, columns)
        if column is not None:
            item = _item(task, board)
            item['is_movable'] = can_work
            item['can_complete'] = task.pk in completable
            cards_by_column[column.pk].append(item)
    for cards in cards_by_column.values():
        cards.sort(key=lambda item: (item['card'].position, item['card'].pk))
    done_column = next((column for column in columns if column.is_done), None)
    done_total = done_tasks.count()
    done_list = []
    if done_column is not None:
        done_list = list(done_tasks.order_by('-completed_at', '-pk')[:done_limit])
        cards_by_column[done_column.pk] = [_item(task, board) for task in done_list]
    working = [column for column in columns if not column.is_done]
    rows = []
    for column in columns:
        cards = cards_by_column[column.pk]
        total = done_total if column.is_done else len(cards)
        index = working.index(column) if not column.is_done else None
        pinned = list(column.pinned_assignees.all()) if not column.is_done else []
        rows.append({
            'column': column,
            'pk': column.pk,
            'name': column.name,
            'is_done': column.is_done,
            'cards': cards,
            'count': total,
            'more': total - len(cards),
            'can_move_left': index is not None and index > 0,
            'can_move_right': index is not None and index < len(working) - 1,
            'can_delete': index is not None and len(working) > 1,
            'pinned': pinned[:PIN_PREVIEW_LIMIT],
            'pinned_more': max(len(pinned) - PIN_PREVIEW_LIMIT, 0),
            'pinned_all': pinned,
            'pinned_ids': {person.pk for person in pinned},
        })
    loaded = {task.board_card_id: task for task in [*open_tasks, *done_list]}
    return {
        'board': board,
        'sub_board': sub_board,
        'sub_boards': [
            {
                'sub_board': tab,
                'is_active': tab.pk == sub_board.pk,
                'can_move_left': index > 0,
                'can_move_right': index < len(tabs) - 1,
                'can_delete': len(tabs) > 1,
            }
            for index, tab in enumerate(tabs)
        ],
        'columns': rows,
        'first_working_column': working[0] if working else None,
        'done_column': done_column,
        'can_add_column': len(columns) < MAX_COLUMNS,
        'member_count': BoardMember.objects.filter(board=board).count(),
        # Whom a column's «Закреплённые исполнители» may name — drawn for the
        # manager only, so nobody else pays for the query.
        'members': (
            list(
                get_user_model().objects.filter(active_employee_q(), board_memberships__board=board)
                .order_by('last_name', 'first_name', 'username', 'pk')
            ) if can_manage and not board.is_archived else []
        ),
        'card': (
            _panel_card(
                board, sub_board, columns_of, card_id, user,
                loaded=loaded, tabs=tabs, can_work=can_work, all_comments=all_comments,
            )
            if card_id not in (None, '') else None
        ),
        'move_choices': _move_choices(tabs, columns_of, sub_board),
        'can_work': can_work,
        'can_manage': can_manage,
        'can_restore': can_restore_board(user, board),
        'filters': filters,
    }


def column_counts(sub_board, user=None, filters=NO_FILTERS):
    """`{column id: number of cards}` — the numbers the column headers show.

    What a drag's JSON answer carries back so the headers can be corrected:
    the open cards per working column (those whose column was deleted count
    in the first one, where they stand), and every completed one for the
    closing column — under the same `filters` the sub-board is drawn with.
    Keys are the column ids as strings, as JSON writes them anyway.
    """
    from tasks.models import Task

    columns = sub_board_columns(sub_board)
    counts = {str(column.pk): 0 for column in columns}
    working = [column for column in columns if not column.is_done]
    first_working_id = working[0].pk if working else None
    tasks = Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__sub_board=sub_board)
    open_cards = (
        _filtered(tasks.filter(status__is_final=False), filters, user, open_work=True)
        .order_by()
        .values('board_card__column_id')
        .annotate(n=Count('pk'))
    )
    for row in open_cards:
        key = str(row['board_card__column_id'] or first_working_id)
        if key in counts:
            counts[key] += row['n']
    done = next((column for column in columns if column.is_done), None)
    if done is not None:
        counts[str(done.pk)] = _filtered(
            tasks.filter(status__code='COMPLETED'), filters, user, open_work=False,
        ).count()
    return counts


def build_board_nav(user, current_board=None):
    """The left panel of every board page: the boards `user` reads.

    One query — the readable boards (`readable_boards_q()`: every board for
    full access, a member's own otherwise) by name — split here into the live
    ones and the archive. `current_id` is the board the page shows, or `None`
    («Новая доска», the empty state). Not live: a board created or archived
    elsewhere shows on the next page.
    """
    boards = list(
        Board.objects.filter(readable_boards_q(user))
        .only('pk', 'name', 'status')
        .order_by('name', 'pk')
    )
    return {
        'boards': [board for board in boards if not board.is_archived],
        'archived': [board for board in boards if board.is_archived],
        'current_id': getattr(current_board, 'pk', None),
    }


def member_preview(board, limit=MEMBER_PREVIEW_LIMIT):
    """The first `limit` members by name, for the avatars in the board heading.

    One query; how many are left over is the caller's `member_count` minus
    the length of this list.
    """
    return [
        member.user
        for member in BoardMember.objects.filter(board=board)
        .select_related('user')
        .order_by('user__last_name', 'user__first_name', 'user__username', 'pk')[:limit]
    ]


def _count_subquery(queryset, group_by):
    """`queryset` (filtered on an `OuterRef`) counted, 0 when empty."""
    counted = queryset.order_by().values(group_by).annotate(n=Count('pk')).values('n')
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


def resolve_new_column(columns, value):
    """The working column `?new=<column id>` names among `columns`, or `None`.

    The closing column, a column of another sub-board and anything that is
    not a number are `None`.
    """
    try:
        column_id = int(value)
    except (TypeError, ValueError):
        return None
    return next(
        (column for column in columns if column.pk == column_id and not column.is_done),
        None,
    )
