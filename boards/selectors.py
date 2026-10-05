"""What the board pages show: the registry, one board's columns, one card.

Read only, and never a permission decision of its own — the flags returned are
`boards.permissions` asked once. The number of queries does not depend on the
number of cards: the open cards and the latest completed ones are each one
query through their tasks, the исполнители one prefetch each, and the card the
panel shows one more.
"""

from django.db.models import Count, IntegerField, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce

from .columns import COLUMNS, DONE, card_column
from .models import Board, BoardMember
from .permissions import can_manage_board, can_work_on_board


# How many completed cards «Готово» draws. The rest are counted, not read:
# a board in use for a year has hundreds of them and nobody scrolls that far.
DONE_LIMIT = 50

REGISTRY_TABS = ('my', 'all')


def _board_tasks(board):
    from tasks.models import Task

    return (
        Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__board=board)
        .select_related('status', 'board_card')
        .prefetch_related('assignees__user__userprofile')
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
    return item


def build_board_state(board, user, *, done_limit=DONE_LIMIT, card_id=None):
    """Everything one board page renders.

    `columns` follows `boards.columns.COLUMNS`; each is
    `{'code', 'label', 'is_stored', 'cards', 'count', 'more'}`, and each card
    is `{'card', 'task', 'assignees', 'due_date', 'is_closed'}`. The working
    columns are in `position` order. «Готово» holds the `done_limit` newest
    completions and `more` counts the rest. A card whose task was cancelled is
    on none of them.

    `card` is the card `card_id` names (see `_panel_card()`), else `None`.
    """
    tasks = _board_tasks(board)
    open_tasks = tasks.filter(status__is_final=False)
    done_tasks = tasks.filter(status__code='COMPLETED')
    cards_by_column = {column.code: [] for column in COLUMNS}
    for task in open_tasks:
        code = card_column(task.board_card, task)
        if code is not None:
            cards_by_column[code].append(_item(task))
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
        'can_work': can_work_on_board(user, board),
        'can_manage': can_manage_board(user, board),
    }


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
    """The registry: «Мои» (boards I am on) and «Все».

    With no tab asked for, «Мои» — unless the user is on no board, when «Все»
    is the only list that says anything.
    """
    mine = boards_for_user(user)
    everything = Board.objects.select_related('department', 'owner').order_by('name', 'pk')
    tab_counts = {'my': mine.count(), 'all': everything.count()}
    if tab not in REGISTRY_TABS:
        tab = 'my' if tab_counts['my'] else 'all'
    boards = _with_counts(mine if tab == 'my' else everything)
    return {'tab': tab, 'tab_counts': tab_counts, 'boards': list(boards)}


def resolve_new_stage(value):
    """The working column `?new=` names, or `None` for «Готово» and anything else."""
    return next(
        (column for column in COLUMNS if column.is_stored and column.code == value),
        None,
    )

