"""What the board pages show: the registry, one board's columns, one card.

Read only, and never a permission decision of its own — the flags returned are
`boards.permissions` asked once. The number of queries does not depend on the
number of cards: the open cards and the latest completed ones are each one
query through their tasks, the исполнители one prefetch each, and the card the
panel shows one more.
"""

from dataclasses import dataclass
from urllib.parse import urlencode

from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from .columns import COLUMNS, DONE, card_column
from .models import Board, BoardCardComment, BoardMember
from .permissions import (
    can_cancel_card,
    can_comment_card,
    can_manage_board,
    can_restore_board,
    can_work_on_board,
)


# How many completed cards «Готово» draws. The rest are counted, not read:
# a board in use for a year has hundreds of them and nobody scrolls that far.
DONE_LIMIT = 50

REGISTRY_TABS = ('my', 'all', 'archive')

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


def _filtered(tasks, filters, user, *, open_work):
    """`tasks` narrowed by `filters`; `overdue` only ever narrows open work.

    «Мои» is «I am an исполнитель» (`TaskAssignee`, one row per person, so the
    join adds no duplicates); `q` is a substring of the card's title.
    """
    if filters.mine:
        tasks = tasks.filter(assignees__user=user)
    if filters.q:
        tasks = tasks.filter(board_card__title__icontains=filters.q)
    if filters.overdue and open_work:
        tasks = tasks.filter(due_date__lt=timezone.localdate())
    return tasks


def _board_tasks(board):
    """The board's tasks with their cards, исполнители and message counts.

    The «Обсуждение» count is a subquery annotation of the same query, so a
    tile's counter costs no query of its own.
    """
    from tasks.models import Task

    comments = BoardCardComment.objects.filter(card=OuterRef('board_card'))
    return (
        Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__board=board)
        .select_related('status', 'board_card')
        .prefetch_related('assignees__user__userprofile')
        .annotate(comment_count=_count_subquery(comments, 'card'))
    )


def _item(task):
    """One card as every board template reads it."""
    card = task.board_card
    return {
        'card': card,
        'task': task,
        'assignees': [assignee.user for assignee in task.assignees.all()],
        'due_date': task.due_date,
        'is_closed': task.status.is_final,
        'comment_count': getattr(task, 'comment_count', 0),
    }


def _panel_card(board, card_id, user):
    """The card `?card=` names, with its task — or `None`.

    `None` for anything that is not a card of this board: a foreign or missing
    id, or text. Unlike the columns, a cancelled card is found too — its panel
    is the read-only record of what was withdrawn.

    The panel is where a `BOARD` task is worked, so the card also carries what
    the task page used to show: its attachments
    (`tasks.presentation.task_attachment_cards()`, the task page's own list)
    and the task rights, each asked once of `tasks.permissions` — the board
    has no rules of its own about completing, reopening or files.
    """
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
    task = (
        _board_tasks(board)
        .select_related('board_card__created_by', 'department', 'completed_by', 'cancelled_by')
        .filter(board_card_id=card_id)
        .first()
    )
    if task is None:
        return None
    item = _item(task)
    code = card_column(task.board_card, task)
    item['column'] = next((column for column in COLUMNS if column.code == code), None)
    item['department'] = task.department
    item['attachments'] = task_attachment_cards(task, user)
    item['can_complete'] = can_complete_task(task, user)
    item['can_reopen'] = can_reopen_task(task, user)
    item['can_upload_attachment'] = can_upload_task_attachment(task, user)
    # `board` is the very board of the page: no second query for it.
    task.board_card.board = board
    item['can_cancel'] = (
        task.status.code == 'IN_PROGRESS' and can_cancel_card(user, task.board_card)
    )
    # «Обсуждение»: every message, oldest first, in one query; writing is
    # `can_comment_card()`, whatever the state of the task.
    item['comments'] = list(
        BoardCardComment.objects.filter(card=task.board_card).select_related('author')
    )
    item['can_comment'] = can_comment_card(user, task.board_card)
    return item


def build_board_state(board, user, *, done_limit=DONE_LIMIT, card_id=None, filters=NO_FILTERS):
    """Everything one board page renders.

    `columns` follows `boards.columns.COLUMNS`; each is
    `{'code', 'label', 'is_stored', 'cards', 'count', 'more'}`, and each card
    is `{'card', 'task', 'assignees', 'due_date', 'is_closed'}`. The working
    columns are in `position` order. «Готово» holds the `done_limit` newest
    completions and `more` counts the rest. A card whose task was cancelled is
    on none of them.

    `card` is the card `card_id` names (see `_panel_card()`), else `None` —
    found whatever the filters say, so the open panel never disappears.

    `filters` (`BoardFilters`) narrows the columns and their counts: «Мои»
    and the search apply to every column, «Просроченные» to the open ones
    only — a completed card is never overdue.

    Each open card also says what this user may do with it by dragging —
    markup only, the routes ask again: `is_movable` (may work on the board)
    and `can_complete` (`tasks.permissions.can_complete_task()`, asked for the
    whole board in one query through `completable_task_ids()`).
    """
    from tasks.permissions import completable_task_ids

    can_work = can_work_on_board(user, board)
    tasks = _board_tasks(board)
    open_tasks = list(_filtered(tasks.filter(status__is_final=False), filters, user, open_work=True))
    done_tasks = _filtered(tasks.filter(status__code='COMPLETED'), filters, user, open_work=False)
    completable = completable_task_ids([task.pk for task in open_tasks], user)
    cards_by_column = {column.code: [] for column in COLUMNS}
    for task in open_tasks:
        code = card_column(task.board_card, task)
        if code is not None:
            item = _item(task)
            item['is_movable'] = can_work
            item['can_complete'] = task.pk in completable
            cards_by_column[code].append(item)
    for cards in cards_by_column.values():
        cards.sort(key=lambda item: (item['card'].position, item['card'].pk))
    cards_by_column[DONE] = [
        _item(task)
        for task in done_tasks.order_by('-completed_at', '-pk')[:done_limit]
    ]
    done_total = done_tasks.count()
    columns = []
    for column in COLUMNS:
        cards = cards_by_column[column.code]
        total = done_total if column.code == DONE else len(cards)
        columns.append({
            'code': column.code,
            'label': column.label,
            'is_stored': column.is_stored,
            'cards': cards,
            'count': total,
            'more': total - len(cards),
        })
    return {
        'board': board,
        'columns': columns,
        'member_count': BoardMember.objects.filter(board=board).count(),
        'card': _panel_card(board, card_id, user) if card_id not in (None, '') else None,
        'can_work': can_work,
        'can_manage': can_manage_board(user, board),
        'can_restore': can_restore_board(user, board),
        'filters': filters,
    }


def column_counts(board, user=None, filters=NO_FILTERS):
    """`{column code: number of cards}` — the numbers the column headers show.

    What a drag's JSON answer carries back so the headers can be corrected:
    the open cards per stored column, and every completed one for «Готово» —
    under the same `filters` the board is drawn with.
    """
    from tasks.models import Task

    counts = {column.code: 0 for column in COLUMNS}
    tasks = Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__board=board)
    open_cards = (
        _filtered(tasks.filter(status__is_final=False), filters, user, open_work=True)
        .order_by()
        .values('board_card__stage')
        .annotate(n=Count('pk'))
    )
    for row in open_cards:
        counts[row['board_card__stage']] = row['n']
    counts[DONE] = _filtered(
        tasks.filter(status__code='COMPLETED'), filters, user, open_work=False,
    ).count()
    return counts


def boards_for_user(user):
    """The boards `user` is a member of, by name."""
    if not getattr(user, 'is_authenticated', False):
        return Board.objects.none()
    return (
        Board.objects.filter(
            pk__in=BoardMember.objects.filter(user=user).values('board_id'),
        )
        .select_related('department', 'owner')
        .order_by('name', 'pk')
    )


def _count_subquery(queryset, group_by):
    """`queryset` (filtered on `OuterRef('pk')`) counted, 0 when empty."""
    counted = queryset.order_by().values(group_by).annotate(n=Count('pk')).values('n')
    return Coalesce(Subquery(counted, output_field=IntegerField()), Value(0))


def _with_counts(boards):
    """Участников and открытых карточек, as annotations of the one query.

    Subqueries rather than `Count()` over joins: «Мои» already filters through
    the membership table, and a join-based count would reuse that join and
    count the viewer alone.
    """
    from tasks.models import Task

    members = BoardMember.objects.filter(board=OuterRef('pk'))
    open_cards = Task.objects.filter(
        source_type=Task.SourceType.BOARD,
        board_card__board=OuterRef('pk'),
        status__code='IN_PROGRESS',
    )
    return boards.annotate(
        member_count=_count_subquery(members, 'board'),
        open_card_count=_count_subquery(open_cards, 'board_card__board'),
    )


def build_board_list_state(user, tab=None):
    """The registry: «Мои» (live boards I am on), «Все» (live) and «Архив».

    With no tab asked for, «Мои» — unless the user is on no live board, when
    «Все» is the only list that says anything.
    """
    everything = Board.objects.select_related('department', 'owner').order_by('name', 'pk')
    lists = {
        'my': boards_for_user(user).filter(status=Board.Status.ACTIVE),
        'all': everything.filter(status=Board.Status.ACTIVE),
        'archive': everything.filter(status=Board.Status.ARCHIVED),
    }
    tab_counts = {name: queryset.count() for name, queryset in lists.items()}
    if tab not in REGISTRY_TABS:
        tab = 'my' if tab_counts['my'] else 'all'
    return {'tab': tab, 'tab_counts': tab_counts, 'boards': list(_with_counts(lists[tab]))}


def resolve_new_stage(value):
    """The working column `?new=` names, or `None` for «Готово» and anything else."""
    return next(
        (column for column in COLUMNS if column.is_stored and column.code == value),
        None,
    )

