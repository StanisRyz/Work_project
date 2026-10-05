"""Simple kanban boards: who works on one and where each card stands.

A card on a board is two rows, and deliberately so. `BoardCard` is the
board's own fact — which column it was put in and in what order — and the work
itself is an ordinary `tasks.Task` with `source_type=BOARD`, created in the
same transaction and linked back through `Task.board_card`. Who does it, by
when, whether it is done and by whom all live on the task, exactly as for every
other source, so «Задачи» and the board can never disagree about the work.

«Готово» is therefore not stored here. `BoardCard.Stage` holds the three
working columns only; a card stands in «Готово» exactly when its task is
`COMPLETED`, which `boards/columns.py` decides. Nothing here writes itself —
every mutation goes through `boards/services.py`.
"""

from django.contrib.auth.models import User
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
    # Organisational metadata, like every `Department` reference: it says whose
    # board this is and becomes the `department` of every task the board
    # creates. It grants nothing — `boards/permissions.py` never reads it.
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='boards',
        verbose_name='Подразделение',
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


class BoardCard(models.Model):
    """Where one piece of board work stands: column and order.

    The work itself — text, срок, исполнители, status — is the `tasks.Task`
    hanging on this card (`Task.board_card`, one per card by
    `unique_board_card_task`). `title`/`description` are what the author typed;
    the task's text is composed from them by `services.compose_task_text()`.
    """

    class Stage(models.TextChoices):
        """The three working columns. «Готово» is deliberately absent.

        A card is in «Готово» exactly when its task is completed, so the
        column is derived (`boards.columns.card_column()`) rather than stored:
        completing the task from its own page and an administrator reopening it
        move the card with no hook in `tasks.services`. The stage a done card
        keeps is the column it returns to when reopened.
        """

        TODO = 'TODO', 'Сделать'
        IN_PROGRESS = 'IN_PROGRESS', 'В работе'
        REVIEW = 'REVIEW', 'На проверке'

    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='cards',
        verbose_name='Доска',
    )
    stage = models.CharField(
        'Колонка', max_length=16, choices=Stage.choices, default=Stage.TODO,
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
        ordering = ['board', 'stage', 'position', 'pk']
        verbose_name = 'Карточка доски'
        verbose_name_plural = 'Карточки досок'
        indexes = [
            models.Index(
                fields=['board', 'stage', 'position'],
                name='board_card_column_order',
            ),
        ]

    def __str__(self):
        return f'Карточка #{self.pk}: {self.title[:60]}'


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
