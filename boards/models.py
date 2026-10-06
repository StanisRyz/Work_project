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

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import Department


class Board(models.Model):
    """One board: a name, an owner and the people allowed to work on it."""

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


class BoardColumn(models.Model):
    """One column of a sub-board, named by its owner.

    The working columns hold the open cards, in `BoardCard.position` order.
    The closing one (`is_done`, exactly one per sub-board, always the last)
    holds the cards whose task is completed — derived, never stored on the
    card — and a drop there is a completion with its result.
    """

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
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['sub_board_id', 'position', 'pk']
        verbose_name = 'Колонка доски'
        verbose_name_plural = 'Колонки досок'
        constraints = [
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

    def __str__(self):
        return f'Карточка #{self.pk}: {self.title[:60]}'

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
