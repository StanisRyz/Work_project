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
from uuid import uuid4

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
    # «Приём заявок»: whether anybody — any active employee, a member or not —
    # may file a request («Заявка», `BoardRequest`) to this board, where an
    # accepted one stands (a working column of `intake_sub_board`; both NULL
    # is the first working column of the first sub-board, and a column
    # deleted meanwhile falls back the same way), how many working days the
    # card gets when the author asks no date (NULL — no default), and the hint
    # above the form. Written only by `services.update_intake()`.
    intake_enabled = models.BooleanField('Приём заявок', default=False)
    intake_sub_board = models.ForeignKey(
        'SubBoard',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Поддоска для заявок',
    )
    intake_column = models.ForeignKey(
        'BoardColumn',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Колонка для заявок',
    )
    intake_due_days = models.PositiveSmallIntegerField(
        'Срок по заявке, рабочих дней', null=True, blank=True,
    )
    intake_hint = models.CharField('Подсказка над формой заявки', max_length=500, blank=True)
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
            models.CheckConstraint(
                condition=models.Q(intake_due_days__isnull=True)
                | models.Q(intake_due_days__lte=365),
                name='board_intake_due_days_valid',
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_archived(self):
        return self.status == self.Status.ARCHIVED


# How many subtasks one card may hold («Подзадачи»).
MAX_SUBTASKS = 50


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


# «Норматив этапа» of a working column: 1 to 365 working days.
NORM_DAYS_MIN = 1
NORM_DAYS_MAX = 365


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
    # «Норматив этапа»: how many working days a card should stand in this
    # working column (`ecosystem.workdays`). Entering the column gives the
    # card a planned exit date — `add_working_days(the day it entered, N)` —
    # and the tile's traffic light reads it; «Просрочен этап» (`?stale=1`)
    # finds the cards past it. NULL is no norm. 1–365, never on the closing
    # column: `services.set_column_norm()` writes it, a check constraint says
    # so. Only the current norm is kept — the stage path reads every past
    # stay against it.
    norm_working_days = models.PositiveSmallIntegerField(
        'Норматив этапа, рабочих дней', null=True, blank=True,
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['sub_board_id', 'position', 'pk']
        verbose_name = 'Колонка доски'
        verbose_name_plural = 'Колонки досок'
        constraints = [
            # A norm is 1–365 working days, and only ever a working column's.
            models.CheckConstraint(
                condition=models.Q(norm_working_days__isnull=True) | models.Q(
                    norm_working_days__gte=NORM_DAYS_MIN,
                    norm_working_days__lte=NORM_DAYS_MAX,
                    is_done=False,
                ),
                name='board_column_norm_days_valid',
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


class BoardColumnSubscription(models.Model):
    """Somebody who asked to be told when a card enters a column («🔔
    Сообщать о новых карточках») — a master of a shop following «Запуск в
    работу» without being anybody's исполнитель.

    Personal: written only by `services.toggle_column_subscription()`, with no
    event and no journal entry; dropped with the membership by
    `remove_board_member()` and with the column itself. Who is told is the
    column's subscribers who still read the board, never whoever moved the
    card (`services._notify_column_entered()`).
    """

    column = models.ForeignKey(
        BoardColumn,
        on_delete=models.CASCADE,
        related_name='subscriptions',
        verbose_name='Колонка',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_column_subscriptions',
        verbose_name='Подписчик',
    )
    created_at = models.DateTimeField('Подписан', auto_now_add=True)

    class Meta:
        ordering = ['column_id', 'created_at', 'pk']
        verbose_name = 'Подписка на колонку'
        verbose_name_plural = 'Подписки на колонки'
        constraints = [
            models.UniqueConstraint(fields=['column', 'user'], name='unique_board_column_subscription'),
        ]

    def __str__(self):
        return f'{self.user} следит за колонкой #{self.column_id}'


class BoardDigestSubscription(models.Model):
    """Somebody who asked for the board's digest by mail («Дайджест на
    почту: ежедневно / еженедельно»), usually a head who does not open the
    system every day.

    Written only by `services.set_digest_subscription()` (any reader of a live
    board), dropped by `remove_board_member()`. Sent by `manage.py
    board_digest` (`boards/digest.py`): `DAILY` on every working day, `WEEKLY`
    on Mondays, one letter per person for all their boards; `last_sent_on` is
    the day it was last handled, so a second run that day sends nothing. The
    digest is a summary, not a business fact: no `Notification` is made.
    """

    class Frequency(models.TextChoices):
        DAILY = 'DAILY', 'Ежедневно'
        WEEKLY = 'WEEKLY', 'Еженедельно'

    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='digest_subscriptions',
        verbose_name='Доска',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_digest_subscriptions',
        verbose_name='Получатель',
    )
    frequency = models.CharField('Как часто', max_length=8, choices=Frequency.choices)
    created_at = models.DateTimeField('Подписан', auto_now_add=True)
    # The day the digest of this subscription was last handled — sent, or
    # found empty — by `board_digest`; NULL until the first run.
    last_sent_on = models.DateField('Обработан за дату', null=True, blank=True)

    class Meta:
        ordering = ['board_id', 'user_id']
        verbose_name = 'Подписка на дайджест доски'
        verbose_name_plural = 'Подписки на дайджест досок'
        constraints = [
            models.UniqueConstraint(fields=['board', 'user'], name='unique_board_digest_subscription'),
            models.CheckConstraint(
                condition=models.Q(frequency__in=['DAILY', 'WEEKLY']),
                name='board_digest_frequency_known',
            ),
        ]

    def __str__(self):
        return f'{self.user}: дайджест доски #{self.board_id} ({self.get_frequency_display()})'


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
    # «Подзадачи»: a subtask is a real card — its own number, исполнители,
    # срок, «Чат» and «Лог», its own `BOARD` task — that lives inside another
    # card instead of a column. One level only: a subtask has no subtasks of
    # its own (`clean()` and `services.create_subtask()`; the database cannot
    # say it without a trigger). A subtask stands on its parent's sub-board
    # with no column (`board_card_subtask_no_column`) and `position` is its
    # place in the parent's list. `PROTECT`: cards are never deleted.
    parent = models.ForeignKey(
        'self',
        on_delete=models.PROTECT,
        related_name='subtasks',
        verbose_name='Карточка',
        null=True,
        blank=True,
    )
    # The card's first срок, written when it is created (`create_card()`,
    # `create_subtask()`). NULL for the cards that existed before stage 21 —
    # their current срок is not necessarily their first, and none is made up:
    # the page reads «—». The срок itself is the task's (`Task.due_date`);
    # every move of it is a `BoardCardDueChange`.
    original_due_date = models.DateField('Исходный срок', null=True, blank=True)
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
            # A subtask lives inside its card, never in a column of the board.
            models.CheckConstraint(
                condition=models.Q(parent__isnull=True) | models.Q(column__isnull=True),
                name='board_subtask_no_column',
            ),
        ]

    def __str__(self):
        return f'Карточка #{self.pk}: {self.title[:60]}'

    @property
    def is_subtask(self):
        return self.parent_id is not None

    @property
    def parent_code(self):
        """«ZAP-12» of the card this subtask lives in, `''` for a card.

        The parent is on the same board, so its code is this board's code and
        the parent's number — a listing that joins `parent` pays no query.
        """
        if self.parent_id is None:
            return ''
        return f'{self.board.code}-{self.parent.number}'

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
        if self.parent_id is not None:
            errors.update(self._subtask_errors())
        if errors:
            raise ValidationError(errors)


    def _subtask_errors(self):
        """A subtask: one level, the parent's board and sub-board, no column."""
        errors = {}
        parent = self.parent
        if self.pk is not None and parent.pk == self.pk:
            errors['parent'] = 'Карточка не может быть подзадачей самой себя.'
        elif parent.parent_id is not None:
            errors['parent'] = 'У подзадачи не бывает подзадач.'
        elif self.pk is not None and BoardCard.objects.filter(parent_id=self.pk).exists():
            errors['parent'] = 'Карточка с подзадачами не может сама стать подзадачей.'
        if parent.board_id != self.board_id or parent.sub_board_id != self.sub_board_id:
            errors['sub_board'] = 'Подзадача стоит на поддоске своей карточки.'
        if self.column_id is not None:
            errors['column'] = 'Подзадача живёт в своей карточке, а не в колонке доски.'
        return errors


# The longest comment a deadline move takes.
DUE_COMMENT_MAX_LENGTH = 500


class BoardCardDueChange(models.Model):
    """One move of a card's срок: was, became, why, who and when.

    Append-only, written by `services.update_card()` alone, in the transaction
    of the edit that moved it. A срок that existed is moved only with a reason
    from `references.DeviationReason`; `reason` is NULL only for a срок set
    where there was none. The comment is the editor's own words, shown on the
    card and in «Отклонения» — never in a notification, a log line or the
    journal's `details`.
    """

    card = models.ForeignKey(
        BoardCard,
        on_delete=models.PROTECT,
        related_name='due_changes',
        verbose_name='Карточка',
    )
    old_due = models.DateField('Было', null=True, blank=True)
    new_due = models.DateField('Стало', null=True, blank=True)
    reason = models.ForeignKey(
        'references.DeviationReason',
        on_delete=models.PROTECT,
        related_name='board_due_changes',
        verbose_name='Причина',
        null=True,
        blank=True,
    )
    comment = models.CharField('Комментарий', max_length=DUE_COMMENT_MAX_LENGTH, blank=True)
    changed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_due_changes',
        verbose_name='Кто перенёс',
    )
    changed_at = models.DateTimeField('Когда', auto_now_add=True)

    class Meta:
        ordering = ['changed_at', 'pk']
        verbose_name = 'Перенос срока карточки'
        verbose_name_plural = 'Переносы сроков карточек'
        indexes = [
            models.Index(fields=['card', 'changed_at'], name='board_due_change_time'),
        ]

    def __str__(self):
        return f'Перенос срока карточки #{self.card_id}'

    @property
    def shift_days(self):
        """How many calendar days later (negative: earlier) the срок became."""
        if self.old_due is None or self.new_due is None:
            return None
        return (self.new_due - self.old_due).days


class BoardCardComment(models.Model):
    """One message of a card's «Чат».

    A record of the discussion: no editing, no deletion. It carries text,
    files (`BoardCardFile`, at most `MAX_FILES_PER_MESSAGE`) or both — the
    text is empty only beside at least one file. People named with «@» are
    its `mentions` (`BoardCardCommentMention`). Written only by
    `services.post_card_comment()`; read by every reader of the board.
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
    # Empty only for a message that is files alone (`post_card_comment()`).
    text = models.TextField('Текст', blank=True)
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


# How many files one message of «Чат» may carry.
MAX_FILES_PER_MESSAGE = 10

# The images «Чат» draws as thumbnails (`boards:file_preview`): the
# extension decides the type served, never the stored `content_type`.
PREVIEW_IMAGE_TYPES = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.webp': 'image/webp',
}


def board_card_file_upload_to(instance, filename):
    """`boards/files/<card_id>/<uuid>.<ext>` — never the browser's name.

    The same shape every attachment in the project has: a UUID name can
    neither collide, escape its directory nor be guessed, and `MEDIA_ROOT`
    is not published — the file is reached only through
    `boards:file_download`/`file_preview`, which ask the right again.
    """
    from ecosystem.attachments import attachment_extension

    card_id = instance.card_id or 'unassigned'
    return f'boards/files/{card_id}/{uuid4().hex}{attachment_extension(filename)}'


class BoardCardFile(models.Model):
    """A file attached to a message of a card's «Чат».

    The board's own file, not the task's (`tasks.TaskAttachment`): whoever
    writes in the chat may attach one, on an open or a closed card alike,
    and reading it is reading the board. Written only by
    `services.post_card_comment()`, with its message, under the one upload
    policy (`ecosystem.attachments`). Deleting it (`delete_card_file()`)
    removes the file from the disk and keeps this row as a tombstone —
    `deleted_at`/`deleted_by`, `file` emptied — so the message still says
    that a file was there.
    """

    card = models.ForeignKey(
        'BoardCard',
        on_delete=models.PROTECT,
        related_name='files',
        verbose_name='Карточка',
    )
    comment = models.ForeignKey(
        'BoardCardComment',
        on_delete=models.PROTECT,
        related_name='files',
        verbose_name='Сообщение',
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_card_files',
        verbose_name='Загрузил',
    )
    file = models.FileField('Файл', upload_to=board_card_file_upload_to, max_length=255, blank=True)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    size = models.PositiveIntegerField('Размер', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    created_at = models.DateTimeField('Загружен', auto_now_add=True)
    deleted_at = models.DateTimeField('Удалён', null=True, blank=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Удалил',
    )

    class Meta:
        ordering = ['comment_id', 'pk']
        verbose_name = 'Файл в чате карточки'
        verbose_name_plural = 'Файлы в чатах карточек'
        indexes = [
            models.Index(fields=['card', 'created_at'], name='board_card_file_time'),
        ]

    def __str__(self):
        return f'Файл #{self.pk} карточки #{self.card_id}'

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    @property
    def extension(self):
        from ecosystem.attachments import attachment_extension

        return attachment_extension(self.original_name)

    @property
    def is_image(self):
        """Whether «Чат» draws it as a thumbnail (`PREVIEW_IMAGE_TYPES`)."""
        return self.extension in PREVIEW_IMAGE_TYPES


# How many items a card's «Чек-лист» may hold.
MAX_CHECKLIST_ITEMS = 50
CHECKLIST_TEXT_MAX_LENGTH = 200


class BoardCardChecklistItem(models.Model):
    """One step of a card's «Чек-лист» — «Согласовать спецификацию», ticked or not.

    A working list, not a record: an item is renamed, reordered and deleted
    for real, and the card's journal (`BoardCardEvent.Kind.CHECKLIST`) is
    what remembers that it happened. At most `MAX_CHECKLIST_ITEMS` per card.
    Who ticked it and when (`done_by`/`done_at`) are kept while it is ticked
    and cleared with the tick. Written only by `boards/services.py`, only on
    an open card of a live board.
    """

    card = models.ForeignKey(
        'BoardCard',
        on_delete=models.PROTECT,
        related_name='checklist',
        verbose_name='Карточка',
    )
    text = models.CharField('Пункт', max_length=CHECKLIST_TEXT_MAX_LENGTH)
    # Order within the card, 1, 2, 3, … — renumbered by every move and delete.
    position = models.PositiveIntegerField('Позиция')
    is_done = models.BooleanField('Сделано', default=False)
    done_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Отметил',
    )
    done_at = models.DateTimeField('Отмечено', null=True, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='+',
        verbose_name='Добавил',
    )
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        ordering = ['card_id', 'position', 'pk']
        verbose_name = 'Пункт чек-листа'
        verbose_name_plural = 'Пункты чек-листов'
        indexes = [
            models.Index(fields=['card', 'position'], name='board_checklist_place'),
        ]

    def __str__(self):
        return f'Пункт #{self.pk} карточки #{self.card_id}'


class BoardCardSubscription(models.Model):
    """Somebody following a card («Следить»): told when it is discussed,
    cancelled or completed, without being its исполнитель.

    Personal: nobody else is told of it and no event is published. Written
    by `services.toggle_card_subscription()` — and by `post_card_comment()`
    for the people a message mentions — and dropped with the membership by
    `remove_board_member()`. Who is told is `selectors.card_audience()`,
    which also leaves out whoever no longer reads the board.
    """

    card = models.ForeignKey(
        'BoardCard',
        on_delete=models.PROTECT,
        related_name='subscriptions',
        verbose_name='Карточка',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_card_subscriptions',
        verbose_name='Подписчик',
    )
    created_at = models.DateTimeField('Подписан', auto_now_add=True)

    class Meta:
        ordering = ['card_id', 'created_at', 'pk']
        verbose_name = 'Подписка на карточку'
        verbose_name_plural = 'Подписки на карточки'
        constraints = [
            models.UniqueConstraint(fields=['card', 'user'], name='unique_board_card_subscription'),
        ]

    def __str__(self):
        return f'{self.user} следит за карточкой #{self.card_id}'


class BoardCardCommentMention(models.Model):
    """A person a message of «Чат» names with «@Имя Фамилия».

    Written with the message by `services.post_card_comment()`, only for a
    reader of the board (an active employee); the message's text shows the
    name highlighted (`boards_mentions` template filter), and the person gets
    one `BOARD_CARD_MENTION`.
    """

    comment = models.ForeignKey(
        'BoardCardComment',
        on_delete=models.PROTECT,
        related_name='mentions',
        verbose_name='Сообщение',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_card_mentions',
        verbose_name='Упомянут',
    )

    class Meta:
        ordering = ['comment_id', 'pk']
        verbose_name = 'Упоминание в сообщении'
        verbose_name_plural = 'Упоминания в сообщениях'
        constraints = [
            models.UniqueConstraint(fields=['comment', 'user'], name='unique_board_comment_mention'),
        ]

    def __str__(self):
        return f'{self.user} в сообщении #{self.comment_id}'


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
        # The card's «Чек-лист»: `details.action` — `added`, `renamed`,
        # `done`, `undone`, `deleted` (a reorder writes no entry) — the item's
        # id and «сделано/всего» after the action, never the item's text.
        CHECKLIST = 'CHECKLIST', 'Чек-лист'
        # The parent's record of its «Подзадачи»: `details.action` — `added`,
        # `completed`, `reopened`, `cancelled` — `subtask_id` and the
        # subtask's `code` («ZAP-13», an identifier), written in the
        # transaction of the subtask's own change.
        SUBTASK = 'SUBTASK', 'Подзадача'
        # «Связи»: a link to another card made or removed — `details.link`
        # (`BLOCKS`/`RELATES`/`DUPLICATES`), `direction` (`out`: this card is
        # the link's `from_card`, `in`: its `to_card`), the other card's
        # `other_id` and `other_code` («СНБ-14», an identifier) — never its
        # title. Written into both cards' journals, in the link's transaction.
        LINKED = 'LINKED', 'Связь добавлена'
        UNLINKED = 'UNLINKED', 'Связь удалена'

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


class BoardCardLink(models.Model):
    """A link between two cards — of one board or of two («Связи»).

    `BLOCKS`: `to_card` waits for `from_card` («СНБ-14 блокирует ZAP-12»,
    ZAP-12 «ждёт» СНБ-14) — a card is blocked while an incoming `BLOCKS`
    comes from a card whose task is still `IN_PROGRESS`. `RELATES`: the two
    are related, the order means nothing, and the services store it one way
    only — the smaller id is `from_card` — so there is never a reverse copy.
    `DUPLICATES`: `from_card` repeats `to_card`.

    Written only by `services.link_cards()`/`unlink_cards()`: created and
    deleted, never edited. Cards are never deleted, so `PROTECT` both ways.
    """

    class Kind(models.TextChoices):
        BLOCKS = 'BLOCKS', 'Блокирует'
        RELATES = 'RELATES', 'Связана'
        DUPLICATES = 'DUPLICATES', 'Дубль'

    from_card = models.ForeignKey(
        BoardCard,
        on_delete=models.PROTECT,
        related_name='outgoing_links',
        verbose_name='От карточки',
    )
    to_card = models.ForeignKey(
        BoardCard,
        on_delete=models.PROTECT,
        related_name='incoming_links',
        verbose_name='К карточке',
    )
    kind = models.CharField('Вид', max_length=12, choices=Kind.choices)
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='+',
        verbose_name='Связал',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']
        verbose_name = 'Связь карточек'
        verbose_name_plural = 'Связи карточек'
        indexes = [
            models.Index(fields=['to_card', 'kind'], name='board_card_link_to'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(from_card=models.F('to_card')),
                name='board_card_link_not_self',
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=['BLOCKS', 'RELATES', 'DUPLICATES']),
                name='board_card_link_kind_known',
            ),
            models.UniqueConstraint(
                fields=['from_card', 'to_card', 'kind'], name='unique_board_card_link',
            ),
        ]

    def __str__(self):
        return f'{self.get_kind_display()}: #{self.from_card_id} → #{self.to_card_id}'


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
    # «Сумма в колонке»: a number field summed in every column header
    # («Σ Сумма: 3 400 000») over the cards the column shows. `NUMBER` only,
    # at most `services.MAX_SUMMED_FIELDS` per board — the services say so,
    # and a check constraint keeps it off every other kind.
    sum_in_column = models.BooleanField('Сумма в колонке', default=False)
    # «Приём заявок»: the field is asked in the request form, and must be
    # filled there. Required only ever in the form — `board_field_required_in_form`.
    in_request_form = models.BooleanField('В форме заявки', default=False)
    required_in_request = models.BooleanField('Обязательно в заявке', default=False)
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
            models.CheckConstraint(
                condition=models.Q(sum_in_column=False) | models.Q(kind='NUMBER'),
                name='board_field_sum_only_number',
            ),
            models.CheckConstraint(
                condition=models.Q(required_in_request=False) | models.Q(in_request_form=True),
                name='board_field_required_in_form',
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


# ---------------------------------------------------------------------------
# «Приём заявок»: requests filed to a board by anybody, and who sorts them
# ---------------------------------------------------------------------------

REQUEST_TITLE_MAX_LENGTH = 200
REQUEST_DESCRIPTION_MAX_LENGTH = 4000
INTAKE_HINT_MAX_LENGTH = 500
INTAKE_DUE_DAYS_MAX = 365


class BoardIntakeHandler(models.Model):
    """A member of the board who is told of every new request («разбирающий»).

    None at all — the owner is told. Written only by
    `services.update_intake()`; `remove_board_member()` drops the person.
    """

    board = models.ForeignKey(
        Board,
        on_delete=models.CASCADE,
        related_name='intake_handlers',
        verbose_name='Доска',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_intake_roles',
        verbose_name='Разбирающий',
    )
    created_at = models.DateTimeField('Назначен', auto_now_add=True)

    class Meta:
        ordering = ['board_id', 'user_id']
        verbose_name = 'Разбирающий заявки доски'
        verbose_name_plural = 'Разбирающие заявки досок'
        constraints = [
            models.UniqueConstraint(fields=['board', 'user'], name='unique_board_intake_handler'),
        ]

    def __str__(self):
        return f'{self.user}: заявки доски #{self.board_id}'


class BoardRequest(models.Model):
    """«Заявка №N»: work somebody asks a board for, member or not.

    Filed by any active employee to a live board with «Приём заявок» on
    (`services.submit_request()`), it waits in the board's «Входящие» as
    `NEW` until a member takes it — `accept_request()` makes a card of it
    (`card`), `reject_request()` says why not (`decision_comment`),
    `mark_duplicate()` names the card it repeats (`duplicate_of`) — or its
    author withdraws it (`withdraw_request()`). Every decision is final and
    records who and when. `field_values` are the board fields of the form,
    `{field id: value}` as the author typed them after the very parse a card's
    values go through. A record: never deleted. Written only by
    `boards/services.py`.
    """

    class Status(models.TextChoices):
        NEW = 'NEW', 'Новая'
        ACCEPTED = 'ACCEPTED', 'Принята'
        REJECTED = 'REJECTED', 'Отклонена'
        DUPLICATE = 'DUPLICATE', 'Дубль'
        WITHDRAWN = 'WITHDRAWN', 'Отозвана'

    board = models.ForeignKey(
        Board,
        on_delete=models.PROTECT,
        related_name='requests',
        verbose_name='Доска',
    )
    author = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='board_requests',
        verbose_name='Автор',
    )
    title = models.CharField('Название', max_length=REQUEST_TITLE_MAX_LENGTH)
    description = models.TextField('Описание', blank=True)
    desired_date = models.DateField('Желаемая дата', null=True, blank=True)
    field_values = models.JSONField('Поля доски', default=dict, blank=True)
    status = models.CharField('Статус', max_length=12, choices=Status.choices, default=Status.NEW)
    card = models.ForeignKey(
        'BoardCard',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='requests',
        verbose_name='Карточка',
    )
    duplicate_of = models.ForeignKey(
        'BoardCard',
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='duplicate_requests',
        verbose_name='Дубль карточки',
    )
    decision_comment = models.TextField('Комментарий к решению', blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name='+',
        verbose_name='Решение принял',
    )
    decided_at = models.DateTimeField('Решение принято', null=True, blank=True)
    created_at = models.DateTimeField('Подана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Заявка на доску'
        verbose_name_plural = 'Заявки на доски'
        indexes = [
            models.Index(fields=['board', 'status', 'created_at'], name='board_request_inbox'),
            models.Index(fields=['author', 'created_at'], name='board_request_author'),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(status__in=['NEW', 'ACCEPTED', 'REJECTED', 'DUPLICATE', 'WITHDRAWN']),
                name='board_request_status_known',
            ),
            # An accepted request has its card, and only an accepted one.
            models.CheckConstraint(
                condition=models.Q(status='ACCEPTED', card__isnull=False)
                | (~models.Q(status='ACCEPTED') & models.Q(card__isnull=True)),
                name='board_request_card_only_when_accepted',
            ),
            # A duplicate names the card it repeats, and only a duplicate.
            models.CheckConstraint(
                condition=models.Q(status='DUPLICATE', duplicate_of__isnull=False)
                | (~models.Q(status='DUPLICATE') & models.Q(duplicate_of__isnull=True)),
                name='board_request_duplicate_only_when_duplicate',
            ),
            # A new request carries no decision; every other one says who and when.
            models.CheckConstraint(
                condition=models.Q(
                    status='NEW', decided_by__isnull=True, decided_at__isnull=True, decision_comment='',
                )
                | (
                    ~models.Q(status='NEW')
                    & models.Q(decided_by__isnull=False, decided_at__isnull=False)
                ),
                name='board_request_decision_matches_status',
            ),
            # A refusal always says why.
            models.CheckConstraint(
                condition=~models.Q(status='REJECTED') | ~models.Q(decision_comment=''),
                name='board_request_rejection_has_reason',
            ),
            models.UniqueConstraint(
                fields=['card'],
                condition=models.Q(card__isnull=False),
                name='unique_board_request_card',
            ),
        ]

    def __str__(self):
        return self.label

    @property
    def label(self):
        return f'Заявка №{self.pk}'

    @property
    def is_new(self):
        return self.status == self.Status.NEW
