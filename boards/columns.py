"""The four columns of a board — the one place they are described.

Three are stored (`BoardCard.Stage`): «Сделать», «В работе», «На проверке».
The fourth, «Готово», is not: a card stands there exactly when its task is
`COMPLETED`. Deriving it rather than storing it is what lets the task's own
lifecycle move the card — «Завершить задачу» on the task page puts it in
«Готово», and an administrator's «Вернуть в работу» (`tasks.services.reopen_task()`)
takes it back to the column its `stage` still names — without a single hook
in `tasks.services`.

A `CANCELLED` task's card is on no column at all: the work was withdrawn, and
showing it anywhere would claim otherwise.
"""

from collections import namedtuple

from .models import BoardCard


DONE = 'DONE'

Column = namedtuple('Column', ('code', 'label', 'is_stored'))

# In the order a board draws them, left to right.
COLUMNS = (
    Column(BoardCard.Stage.TODO.value, BoardCard.Stage.TODO.label, True),
    Column(BoardCard.Stage.IN_PROGRESS.value, BoardCard.Stage.IN_PROGRESS.label, True),
    Column(BoardCard.Stage.REVIEW.value, BoardCard.Stage.REVIEW.label, True),
    Column(DONE, 'Готово', False),
)

# The columns a card may be put in by hand: `create_card()` and `move_card()`
# accept these and nothing else.
WORK_STAGES = frozenset(column.code for column in COLUMNS if column.is_stored)


def card_column(card, task):
    """Which column `card` stands in, given its task — or `None` if none.

    `DONE` for a completed task, `None` for a cancelled one, otherwise the
    stored `card.stage`.
    """
    code = task.status.code
    if code == 'COMPLETED':
        return DONE
    if code == 'CANCELLED':
        return None
    return card.stage
