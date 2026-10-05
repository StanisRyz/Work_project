"""What a board shows: its four columns, each with its cards.

Read only, and never a permission decision of its own — the two flags it
returns are `boards.permissions` asked once. The number of queries does not
depend on the number of cards: the cards are read through their tasks in one
query, and the исполнители in one prefetch.
"""

from .columns import COLUMNS, DONE, card_column
from .models import Board
from .permissions import can_manage_board, can_work_on_board


def _card_tasks(board):
    from tasks.models import Task

    return (
        Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__board=board)
        .select_related('status', 'board_card')
        .prefetch_related('assignees__user__userprofile')
    )


def build_board_state(board, user):
    """`{'board', 'columns', 'can_work', 'can_manage'}` for one board.

    `columns` follows `boards.columns.COLUMNS`; each is
    `{'code', 'label', 'cards'}`, and each card is
    `{'card', 'task', 'assignees', 'due_date'}`. The working columns are in
    `position` order, «Готово» newest completion first, and a card whose task
    was cancelled is on none of them.
    """
    cards_by_column = {column.code: [] for column in COLUMNS}
    for task in _card_tasks(board):
        card = task.board_card
        code = card_column(card, task)
        if code is None:
            continue
        cards_by_column[code].append({
            'card': card,
            'task': task,
            'assignees': [assignee.user for assignee in task.assignees.all()],
            'due_date': task.due_date,
        })
    for code, cards in cards_by_column.items():
        if code == DONE:
            cards.sort(key=lambda item: (item['task'].completed_at, item['card'].pk), reverse=True)
        else:
            cards.sort(key=lambda item: (item['card'].position, item['card'].pk))
    return {
        'board': board,
        'columns': [
            {'code': column.code, 'label': column.label, 'cards': cards_by_column[column.code]}
            for column in COLUMNS
        ],
        'can_work': can_work_on_board(user, board),
        'can_manage': can_manage_board(user, board),
    }


def boards_for_user(user):
    """The boards `user` is a member of, by name."""
    if not getattr(user, 'is_authenticated', False):
        return Board.objects.none()
    return (
        Board.objects.filter(members__user=user)
        .select_related('department', 'owner')
        .order_by('name', 'pk')
        .distinct()
    )
