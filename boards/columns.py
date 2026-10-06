"""Which column a card stands in — and the columns a new sub-board starts with.

The columns themselves are rows (`BoardColumn`), named and ordered by the
board's owner. What stays fixed is the rule tying a card to one of them:

* a `COMPLETED` task stands in the sub-board's closing column (`is_done`) —
  derived, never stored, so «Завершить задачу» on the task page puts it there
  and an administrator's «Вернуть в работу» (`tasks.services.reopen_task()`)
  takes it back, without a single hook in `tasks.services`;
* a `CANCELLED` task stands in no column at all: the work was withdrawn, and
  showing it anywhere would claim otherwise;
* any other stands in `card.column`, its working column — or, when that was
  deleted while the card was closed (`column` is NULL), in the first working
  column of its sub-board. Reopening such a card puts it there too.
"""


# The most columns one sub-board may have, the closing one included.
MAX_COLUMNS = 15

# A new sub-board's columns, left to right: (name, is_done). Exactly one
# closing column, and it is the last.
DEFAULT_COLUMNS = (
    ('Сделать', False),
    ('В работе', False),
    ('На проверке', False),
    ('Готово', True),
)


def card_column(card, task, columns):
    """The `BoardColumn` `card` stands in, given its task — or `None`.

    `columns` is its sub-board's columns in order (the caller reads them once
    for the whole sub-board, so this costs no query).
    """
    code = task.status.code
    if code == 'CANCELLED':
        return None
    if code == 'COMPLETED':
        return next((column for column in columns if column.is_done), None)
    working = [column for column in columns if not column.is_done]
    return next(
        (column for column in working if column.pk == card.column_id),
        working[0] if working else None,
    )
