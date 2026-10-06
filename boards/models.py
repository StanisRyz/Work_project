"""Simple kanban boards: who works on one and where each card stands.

A card on a board is two rows, and deliberately so. `BoardCard` is the
board's own fact — which column it was put in and in what order — and the work
itself is an ordinary `tasks.Task` with `source_type=BOARD`, created in the
same transaction and linked back through `Task.board_card`. Who does it, by
when, whether it is done and by whom all live on the task, exactly as for every
other source, so «Задачи» and the board can never disagree about the work.

A board is split into sub-boards (`SubBoard`, the tabs above the columns),
and each sub-board has its own columns (`BoardColumn`) with any names its
owner gives them. Exactly one column of a sub-board is the closing one
(`is_done`), and it is not stored on a card either: a card stands there
exactly when its task is `COMPLETED`, which `boards/columns.py` decides, and
`BoardCard.column` only ever names a working column — the one the card stands
in while open and returns to when reopened. Nothing here writes itself —
every mutation goes through `boards/services.py`.
"""

import re

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import Department


# A board's code: «ZAP», «ПДО», «D7». Two to six letters (Latin or Cyrillic)
# and digits, upper case — the form `services.clean_board_code()` stores.
BOARD_CODE_MIN_LENGTH = 2
BOARD_CODE_MAX_LENGTH = 6
BOARD_CODE_PATTERN = re.compile(r'^[0-9A-ZА-ЯЁ]{2,6}$')


class Board(models.Model):
    """One board: a name, a code, an owner and the people allowed to work on it."""

    class Status(models.TextChoices):
        """A shelf, not a workflow — like `smk.SmkSource.Status`.

        An archived board is read at the same address and changes nothing:
        `boards.permissions` refuses every write to it, and only «Вернуть из
        архива» moves it back. `archive_board()` refuses a board with open
        cards, so the shelf never hides work still in progress.
        """

        ACTIVE = 'ACTIVE', 'Активна'
        ARCHIVED = 'ARCHIVED', 'В архиве'

    name = models.CharField('Название', max_length=200)
    # The short code every card number of the board starts with — «ZAP» in
    # «ZAP-12». Two to six letters (Latin or Cyrillic) and digits, stored in
    # upper case, so the plain unique constraint is uniqueness whatever the
    # case was typed in: `services.clean_board_code()` is the one normaliser.
    code = models.CharField('Код', max_length=BOARD_CODE_MAX_LENGTH)
    description = models.TextField('Описание', blank=True)
    # Kept only for the boards that already carry one: a board is shared work
    # of people from any number of departments, so a new board names none and
    # its tasks name none either. It grants nothing — `boards/permissions.py`
    # never reads it — and a page shows nothing where it is empty.
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='boards',
        verbose_name='Подразделение',
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='owned_boards',
        verbose_name='Владелец',
    )
    status = models.CharField(
        'Статус', max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )
    archived_at = models.DateTimeField('В архиве с', null=True, blank=True)
    archived_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Убрал в архив',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'Доска'
        verbose_name_plural = 'Доски'
        constraints = [
            # Stored upper case (`services.clean_board_code()`), so this is
            # uniqueness without regard to case.
            models.UniqueConstraint(fields=['code'], name='unique_board_code'),
        ]

    def __str__(self):
        return self.name

    @property
    def is_archived(self):
        return self.status == self.Status.ARCHIVED


class BoardMember(models.Model):
    """A person who may put cards on the board and move them.

    The owner is always one of them: `create_board()` adds them and
    `remove_board_member()` refuses to take them off.
    """

    board = models.ForeignKey(
        Board,
        on_delete=models.CASCADE,
        related_name='members',
        verbose_name='Доска',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_memberships',
        verbose_name='Участник',
    )
    added_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Добавил',
    )
    added_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        ordering = ['board', 'pk']
        verbose_name = 'Участник доски'
        verbose_name_plural = 'Участники досок'
        constraints = [
            models.UniqueConstraint(fields=['board', 'user'], name='unique_board_member'),
        ]

    def __str__(self):
        return f'{self.board}: {self.user}'


class SubBoard(models.Model):
    """One tab of a board: its own columns and the cards standing in them.

    Members, the owner, the archive and every right stay on the `Board`; a
    sub-board only divides its work. `create_board()` gives every board one,
    «Основная», and a board always keeps at least one
    (`services.delete_sub_board()` refuses the last).
    """

    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='sub_boards',
        verbose_name='Доска',
    )
    name = models.CharField('Название', max_length=100)
    # Order of the tabs, 1, 2, 3, … — renumbered by every move.
    position = models.PositiveIntegerField('Позиция')
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='+',
        verbose_name='Создал',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['board_id', 'position', 'pk']
        verbose_name = 'Поддоска'
        verbose_name_plural = 'Поддоски'
        constraints = [
            models.UniqueConstraint(fields=['board', 'name'], name='unique_sub_board_name'),
        ]

    def __str__(self):
        return f'{self.board}: {self.name}'


# «Застой» of a working column: a threshold of 1 to 365 calendar days.
STALE_DAYS_MIN = 1
STALE_DAYS_MAX = 365


class BoardColumn(models.Model):
    """One column of a sub-board, named by its owner.

    The working columns hold the open cards, in `BoardCard.position` order.
    The closing one (`is_done`, exactly one per sub-board, always the last)
    holds the cards whose task is completed — derived, never stored on the
    card — and a drop there is a completion with its result.
    """

    class PinnedMode(models.TextChoices):
        ADD = 'ADD', 'Добавить к исполнителям'
        REPLACE = 'REPLACE', 'Заменить исполнителей'

    sub_board = models.ForeignKey(
        SubBoard,
        on_delete=models.PROTECT,
        related_name='columns',
        verbose_name='Поддоска',
    )
    name = models.CharField('Название', max_length=60)
    # Order within the sub-board, 1, 2, 3, … — renumbered by every change;
    # the closing column is always the last.
    position = models.PositiveIntegerField('Позиция')
    is_done = models.BooleanField('Завершающая', default=False)
    # «Закреплённые исполнители»: board members a card gets when it is created
    # in this column or moved into it — added to its исполнители (`ADD`) or
    # put in their place (`REPLACE`). A working column's only; the cards
    # already standing here when the pins change keep their people.
    # `services.set_column_pins()` writes them, `services._apply_pins()` uses
    # them, `remove_board_member()` drops a removed member's.
    pinned_assignees = models.ManyToManyField(
        User,
        through='BoardColumnPin',
        related_name='pinned_board_columns',
        verbose_name='Закреплённые исполнители',
        blank=True,
    )
    pinned_mode = models.CharField(
        'Режим закрепления', max_length=8, choices=PinnedMode.choices, default=PinnedMode.ADD,
    )
    # «Застой»: a working column's threshold in calendar days — a card that has
    # stood here at least this long is highlighted on its tile, and «Застрявшие»
    # (`?stale=1`) finds it. NULL is off. 1–365, never on the closing column:
    # `services.set_column_stale_days()` writes it, a check constraint says so.
    stale_after_days = models.PositiveSmallIntegerField(
        'Застой: подсвечивать через, дней', null=True, blank=True,
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['sub_board_id', 'position', 'pk']
        verbose_name = 'Колонка доски'
        verbose_name_plural = 'Колонки досок'
        constraints = [
            # A threshold is 1–365 days, and only ever a working column's.
            models.CheckConstraint(
                condition=models.Q(stale_after_days__isnull=True) | models.Q(
                    stale_after_days__gte=STALE_DAYS_MIN,
                    stale_after_days__lte=STALE_DAYS_MAX,
                    is_done=False,
                ),
                name='board_column_stale_days_valid',
            ),
            # At most one closing column per sub-board; the services create
            # it with the sub-board and never delete it, so there is exactly one.
            models.UniqueConstraint(
                fields=['sub_board'],
                condition=models.Q(is_done=True),
                name='unique_done_column_per_sub_board',
            ),
        ]

    def __str__(self):
        return f'{self.sub_board}: {self.name}'


class BoardColumnPin(models.Model):
    """One person pinned to a working column (`BoardColumn.pinned_assignees`)."""

    column = models.ForeignKey(
        BoardColumn,
        on_delete=models.CASCADE,
        related_name='pins',
        verbose_name='Колонка',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_column_pins',
        verbose_name='Сотрудник',
    )

    class Meta:
        ordering = ['column', 'pk']
        verbose_name = 'Закреплённый исполнитель колонки'
        verbose_name_plural = 'Закреплённые исполнители колонок'
        constraints = [
            models.UniqueConstraint(fields=['column', 'user'], name='unique_board_column_pin'),
        ]

    def __str__(self):
        return f'{self.column}: {self.user}'


class BoardCard(models.Model):
    """Where one piece of board work stands: column and order.

    The work itself — text, срок, исполнители, status — is the `tasks.Task`
    hanging on this card (`Task.board_card`, one per card by
    `unique_board_card_task`). `title`/`description` are what the author typed;
    the task's text is composed from them by `services.compose_task_text()`.
    """

    # Rights, locks and events hang on the board; it always is
    # `sub_board.board` (`clean()` and every service check it).
    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='cards',
        verbose_name='Доска',
    )
    sub_board = models.ForeignKey(
        SubBoard,
        on_delete=models.PROTECT,
        related_name='cards',
        verbose_name='Поддоска',
    )
    # Always a working column of `sub_board` — never the closing one, which
    # is derived from the task. NULL only after its column was deleted while
    # the card was closed: it then returns to the first working column.
    column = models.ForeignKey(
        BoardColumn,
        on_delete=models.SET_NULL,
        related_name='cards',
        verbose_name='Колонка',
        null=True,
        blank=True,
    )
    # Order within the column, spaced by `services.POSITION_STEP` so a move
    # usually writes one row; the column is renumbered only when two
    # neighbours have no gap left between them.
    position = models.PositiveIntegerField('Позиция')
    # The card's number on its board — «12» in «ZAP-12». `create_card()` gives
    # the next one under the board lock (the largest so far plus one), and it
    # is never reused: a cancelled card keeps its number, and no card is ever
    # deleted. Moving the card between sub-boards keeps it too.
    number = models.PositiveIntegerField('Номер')
    title = models.CharField('Заголовок', max_length=200)
    description = models.TextField('Описание', blank=True)
    # Grows by one with every edit that stored something (`update_card()`), and
    # only then: a move or a completion does not touch the text an editor is
    # holding. The edit form carries the number it was drawn with, and
    # `update_card(expected_version=…)` refuses a save made against an older
    # one — two people editing one card never silently overwrite each other.
    version = models.PositiveIntegerField('Версия', default=1)
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='created_board_cards',
        verbose_name='Создал',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['sub_board_id', 'column_id', 'position', 'pk']
        verbose_name = 'Карточка доски'
        verbose_name_plural = 'Карточки досок'
        indexes = [
            models.Index(
                fields=['sub_board', 'column', 'position'],
                name='board_card_place',
            ),
        ]
        constraints = [
            models.UniqueConstraint(fields=['board', 'number'], name='unique_board_card_number'),
        ]

    def __str__(self):
        return f'Карточка #{self.pk}: {self.title[:60]}'

    @property
    def code(self):
        """«ZAP-12»: the board's code and the card's number — how people name it.

        Reads `self.board`; a listing attaches the board it already holds, so
        this costs no query per card.
        """
        return f'{self.board.code}-{self.number}'

    def clean(self):
        """The card's board, sub-board and column must agree.

        `board` is `sub_board.board`, and `column` — when set — is a working
        column of `sub_board`. The services check the same under the board
        lock; this is what Admin and a hand-written save would hit.
        """
        errors = {}
        if self.sub_board_id is not None and self.board_id is not None:
            if self.sub_board.board_id != self.board_id:
                errors['sub_board'] = 'Поддоска принадлежит другой доске.'
        if self.column_id is not None and self.sub_board_id is not None:
            if self.column.sub_board_id != self.sub_board_id:
                errors['column'] = 'Колонка принадлежит другой поддоске.'
            elif self.column.is_done:
                errors['column'] = (
                    'Карточка стоит в завершающей колонке только по выполненной задаче.'
                )
        if errors:
            raise ValidationError(errors)


class BoardCardComment(models.Model):
    """One message of a card's «Обсуждение».

    A record of the discussion, not a chat: no editing, no deletion, no files
    or mentions. Written only by `services.post_card_comment()`; read by every
    reader of the board.
    """

    card = models.ForeignKey(
        BoardCard,
        on_delete=models.PROTECT,
        related_name='comments',
        verbose_name='Карточка',
    )
    author = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_card_comments',
        verbose_name='Автор',
    )
    text = models.TextField('Текст')
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        verbose_name = 'Сообщение в карточке'
        verbose_name_plural = 'Сообщения в карточках'
        indexes = [
            models.Index(fields=['card', 'created_at'], name='board_card_comment_time'),
        ]

    def __str__(self):
        return f'Сообщение #{self.pk} в карточке #{self.card_id}'


class BoardCardEvent(models.Model):
    """One entry of a card's journal — «Лог» in the card panel.

    Append-only: written only by `boards/services.py`, inside the transaction
    of the change it records, so a rolled-back change takes its entry with it;
    there is no edit and no delete. `details` holds identifiers and the names
    of columns *as they were* — a column is renamed or deleted later, and the
    entry must keep saying where the card went — never the text of the card,
    a reason or a message. Files are not recorded here: the panel reads them
    off `TaskAttachment` beside these entries.
    """

    class Kind(models.TextChoices):
        CREATED = 'CREATED', 'Создание'
        EDITED = 'EDITED', 'Изменение'
        MOVED = 'MOVED', 'Перенос'
        COMPLETED = 'COMPLETED', 'Завершение'
        REOPENED = 'REOPENED', 'Возврат в работу'
        CANCELLED = 'CANCELLED', 'Отмена'

    card = models.ForeignKey(
        BoardCard,
        on_delete=models.PROTECT,
        related_name='events',
        verbose_name='Карточка',
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_card_events',
        verbose_name='Кто',
    )
    kind = models.CharField('Событие', max_length=20, choices=Kind.choices)
    details = models.JSONField('Подробности', default=dict, blank=True)
    created_at = models.DateTimeField('Когда', auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        verbose_name = 'Событие карточки'
        verbose_name_plural = 'Журнал карточек'
        indexes = [
            models.Index(fields=['card', 'created_at'], name='board_card_event_time'),
        ]

    def __str__(self):
        return f'{self.get_kind_display()} карточки #{self.card_id}'


# --------------------------------------------------------------------------
# Custom card fields
# --------------------------------------------------------------------------
#
# A board's own fields («Номер заявки», «Срок изг.», «Приоритет»): defined
# once on the board and shared by every sub-board, each card holding at most
# one value of each. An empty value is no row at all. A field or an option
# that a value names is never deleted — it is archived: no longer offered,
# no longer on a tile, but the value stays and «Описание» shows it marked
# «(в архиве)». `boards/services.py` is the only writer.


class BoardFieldColor(models.TextChoices):
    """The closed set of colours a list option may carry.

    Each is a pair of tokens in `static/css/boards.css`
    (`--board-color-<code>-bg` / `-text`), drawn through `.board-chip--<code>`.
    """

    GRAY = 'gray', 'Серый'
    BLUE = 'blue', 'Синий'
    GREEN = 'green', 'Зелёный'
    YELLOW = 'yellow', 'Жёлтый'
    ORANGE = 'orange', 'Оранжевый'
    RED = 'red', 'Красный'
    PURPLE = 'purple', 'Фиолетовый'
    TEAL = 'teal', 'Бирюзовый'


class BoardField(models.Model):
    """One field of a board's cards, shared by all its sub-boards.

    At most `services.MAX_FIELDS` live (not archived) fields per board; the
    name is unique on the board whatever the case (`services`, by
    `casefold()`, as sub-boards are). `kind` changes only while no card holds
    a value of it, and a field with values is archived, never deleted.
    """

    class Kind(models.TextChoices):
        TEXT = 'TEXT', 'Текст'
        NUMBER = 'NUMBER', 'Число'
        DATE = 'DATE', 'Дата'
        SELECT = 'SELECT', 'Список'

    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='fields',
        verbose_name='Доска',
    )
    name = models.CharField('Название', max_length=60)
    kind = models.CharField('Вид', max_length=8, choices=Kind.choices)
    # Order on the card and on the tile, 1, 2, 3, … — renumbered by every move.
    position = models.PositiveIntegerField('Позиция')
    show_on_tile = models.BooleanField('Показывать на плитке', default=True)
    is_archived = models.BooleanField('В архиве', default=False)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        ordering = ['board_id', 'position', 'pk']
        verbose_name = 'Поле карточек'
        verbose_name_plural = 'Поля карточек'
        constraints = [
            models.CheckConstraint(
                condition=models.Q(kind__in=['TEXT', 'NUMBER', 'DATE', 'SELECT']),
                name='board_field_kind_known',
            ),
        ]

    def __str__(self):
        return f'{self.board}: {self.name}'


class BoardFieldOption(models.Model):
    """One choice of a list field («Высокий», red). At most
    `services.MAX_OPTIONS` live ones per field; one a value names is
    archived, never deleted."""

    field = models.ForeignKey(
        BoardField,
        on_delete=models.CASCADE,
        related_name='options',
        verbose_name='Поле',
    )
    label = models.CharField('Подпись', max_length=60)
    color = models.CharField(
        'Цвет', max_length=10, choices=BoardFieldColor.choices, default=BoardFieldColor.GRAY,
    )
    position = models.PositiveIntegerField('Позиция')
    is_archived = models.BooleanField('В архиве', default=False)

    class Meta:
        ordering = ['field_id', 'position', 'pk']
        verbose_name = 'Вариант поля'
        verbose_name_plural = 'Варианты полей'
        constraints = [
            # The colour becomes a CSS class (`board-chip--<code>`): only the
            # eight the stylesheet declares.
            models.CheckConstraint(
                condition=models.Q(color__in=BoardFieldColor.values),
                name='board_field_option_color_known',
            ),
        ]

    def __str__(self):
        return f'{self.field}: {self.label}'


class BoardCardFieldValue(models.Model):
    """The value one card holds for one field — exactly one column filled.

    `value_text` for `TEXT`, `value_number` for `NUMBER`, `value_date` for
    `DATE`, `option` for `SELECT`. An empty value is no row: the services
    delete it. The check constraint says «exactly one column», `clean()` and
    the services say «the column of the field's kind».
    """

    card = models.ForeignKey(
        BoardCard,
        # Cards are never deleted; the cascade only completes the schema.
        on_delete=models.CASCADE,
        related_name='field_values',
        verbose_name='Карточка',
    )
    field = models.ForeignKey(
        BoardField,
        on_delete=models.PROTECT,
        related_name='values',
        verbose_name='Поле',
    )
    value_text = models.CharField('Текст', max_length=500, null=True, blank=True)
    value_number = models.DecimalField('Число', max_digits=18, decimal_places=4, null=True, blank=True)
    value_date = models.DateField('Дата', null=True, blank=True)
    option = models.ForeignKey(
        BoardFieldOption,
        on_delete=models.PROTECT,
        related_name='values',
        verbose_name='Вариант',
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ['card_id', 'field_id']
        verbose_name = 'Значение поля карточки'
        verbose_name_plural = 'Значения полей карточек'
        constraints = [
            models.UniqueConstraint(fields=['card', 'field'], name='unique_board_card_field_value'),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        value_text__isnull=False, value_number__isnull=True,
                        value_date__isnull=True, option__isnull=True,
                    ) & ~models.Q(value_text='')
                    | models.Q(
                        value_text__isnull=True, value_number__isnull=False,
                        value_date__isnull=True, option__isnull=True,
                    )
                    | models.Q(
                        value_text__isnull=True, value_number__isnull=True,
                        value_date__isnull=False, option__isnull=True,
                    )
                    | models.Q(
                        value_text__isnull=True, value_number__isnull=True,
                        value_date__isnull=True, option__isnull=False,
                    )
                ),
                name='board_card_field_value_exactly_one',
            ),
        ]

    def __str__(self):
        return f'{self.field.name} карточки #{self.card_id}'

    # The column each kind is stored in.
    KIND_COLUMNS = {
        BoardField.Kind.TEXT: 'value_text',
        BoardField.Kind.NUMBER: 'value_number',
        BoardField.Kind.DATE: 'value_date',
        BoardField.Kind.SELECT: 'option',
    }

    def clean(self):
        """The field is of the card's board, and its kind's column is the filled one."""
        errors = {}
        if self.field_id is not None and self.card_id is not None:
            if self.field.board_id != self.card.board_id:
                errors['field'] = 'Поле принадлежит другой доске.'
        if self.field_id is not None:
            expected = self.KIND_COLUMNS.get(self.field.kind)
            filled = [
                name for name in ('value_text', 'value_number', 'value_date', 'option')
                if getattr(self, name if name != 'option' else 'option_id') not in (None, '')
            ]
            if filled != [expected]:
                errors['field'] = 'Значение не соответствует виду поля.'
            if self.option_id is not None and self.option.field_id != self.field_id:
                errors['option'] = 'Вариант принадлежит другому полю.'
        if errors:
            raise ValidationError(errors)
