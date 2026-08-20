"""Domain model of a meeting protocol.

The protocol owns its type, its per-type number, its author, its status and
its revision; the meeting itself lives in the child rows — participants,
«Повестка», «Слушали» and the task drafts. Nothing here writes itself: every
mutation goes through `protocols/services.py`, so a fixture load or a
technical `save()` never allocates a number or invents a history event.

`ProtocolAction` is deliberately *not* `tasks.Task`. It is the decision as it
is recorded inside the protocol; turning one into a real task is a later stage
and a separate model, so the two schemas stay independent.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models

from accounts.models import Department


# The first protocol kind, seeded by a data migration. The code is the stable
# identifier business logic may key on; the primary key never is.
QUALITY_PROTOCOL_TYPE_CODE = 'QUALITY'


class ProtocolType(models.Model):
    """A kind of protocol, owning its own independent number series.

    A first-class model rather than a `TextChoices` field: numbering, and later
    the approval rules, are per type, and a new kind must be addable without a
    schema migration.
    """

    code = models.CharField('Код', max_length=32, unique=True)
    name = models.CharField('Название', max_length=120)
    is_active = models.BooleanField('Активен', default=True)
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['display_order', 'name']
        verbose_name = 'Тип протокола'
        verbose_name_plural = 'Типы протоколов'

    def __str__(self):
        return self.name


class Protocol(models.Model):
    """One meeting protocol of a given type.

    `number` is unique per type only, and it is *reusable*: deleting a draft
    releases it, and the next protocol of that type takes the smallest free
    positive number. The number is allocated exclusively by
    `protocols.services.create_protocol()` under a row lock on the type; the
    unique constraint below is the last line of defence, not the allocator.
    """

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Черновик'
        APPROVAL = 'APPROVAL', 'На согласовании'
        REVISION = 'REVISION', 'На доработке'
        ARCHIVED = 'ARCHIVED', 'В архиве'

    protocol_type = models.ForeignKey(
        ProtocolType,
        on_delete=models.PROTECT,
        related_name='protocols',
        verbose_name='Тип протокола',
    )
    number = models.PositiveIntegerField('Номер')
    author = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='authored_protocols',
        verbose_name='Автор',
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    # 0 while the protocol has never left the author: the first submission for
    # approval is what makes it revision 1.
    revision = models.PositiveIntegerField('Редакция', default=0)
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Протокол'
        verbose_name_plural = 'Протоколы'
        constraints = [
            models.UniqueConstraint(
                fields=['protocol_type', 'number'],
                name='unique_protocol_number_per_type',
            )
        ]

    def __str__(self):
        return f'{self.protocol_type.name} №{self.number}'


class ProtocolParticipant(models.Model):
    """A person taking part in the meeting, with the snapshot the archive needs.

    `display_name`, `position` and `department_name` are copied from the user's
    profile when the participant is added and never follow it afterwards: an
    archived protocol must keep saying who took part and in which role, even
    after a transfer or a rename.

    There is no `is_author` flag on purpose — `Protocol.author` is the only
    authoritative answer, and a second copy of it would be a second truth.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='participants',
        verbose_name='Протокол',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='protocol_participations',
        verbose_name='Пользователь',
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='protocol_participations',
        verbose_name='Подразделение',
        blank=True,
        null=True,
    )
    requires_approval = models.BooleanField('Требуется согласование', default=False)
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)
    display_name = models.CharField('ФИО в документе', max_length=180)
    position = models.CharField('Должность в документе', max_length=120, blank=True)
    department_name = models.CharField('Подразделение в документе', max_length=120, blank=True)
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Участник протокола'
        verbose_name_plural = 'Участники протоколов'
        constraints = [
            models.UniqueConstraint(
                fields=['protocol', 'user'],
                name='unique_protocol_participant',
            )
        ]

    def __str__(self):
        return f'{self.protocol}: {self.display_name}'


class ProtocolAgendaItem(models.Model):
    """One «Повестка» line.

    The future form will require at least one item; that is a form rule, not a
    cross-row database constraint, so the table stays a plain ordered list.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='agenda_items',
        verbose_name='Протокол',
    )
    text = models.TextField('Вопрос повестки')
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Вопрос повестки'
        verbose_name_plural = 'Вопросы повестки'

    def __str__(self):
        return f'{self.protocol}: {self.text[:60]}'


class ProtocolSpeech(models.Model):
    """One «Слушали» entry.

    The speaker is a `ProtocolParticipant`, never a bare `User`: only someone
    present at the meeting can have spoken at it, and the relation is what
    makes that checkable instead of merely intended.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='speeches',
        verbose_name='Протокол',
    )
    speaker = models.ForeignKey(
        ProtocolParticipant,
        on_delete=models.PROTECT,
        related_name='speeches',
        verbose_name='Выступающий',
    )
    text = models.TextField('Слушали')
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Выступление'
        verbose_name_plural = 'Выступления'

    def __str__(self):
        return f'{self.protocol}: {self.text[:60]}'

    def clean(self):
        # A composite foreign key would express this in the schema; Django has
        # none, so the rule lives here and in the service that writes speeches.
        super().clean()
        if (
            self.speaker_id is not None
            and self.protocol_id is not None
            and self.speaker.protocol_id != self.protocol_id
        ):
            raise ValidationError(
                {'speaker': 'Выступающий должен быть участником этого протокола.'}
            )


class ProtocolAction(models.Model):
    """A decision recorded in the protocol as a future task.

    Not a `tasks.Task` and not linked to one: real tasks are created by a later
    stage from an archived protocol, and until then this table carries the
    wording, the department and the deadline on its own.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='actions',
        verbose_name='Протокол',
    )
    task_text = models.TextField('Задача')
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='protocol_actions',
        verbose_name='Подразделение',
    )
    due_date = models.DateField('Срок')
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Задача протокола'
        verbose_name_plural = 'Задачи протоколов'

    def __str__(self):
        return f'{self.protocol}: {self.task_text[:60]}'


class ProtocolActionAssignee(models.Model):
    action = models.ForeignKey(
        ProtocolAction,
        on_delete=models.CASCADE,
        related_name='assignees',
        verbose_name='Задача протокола',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='protocol_action_assignments',
        verbose_name='Исполнитель',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['action', 'user'],
                name='unique_protocol_action_assignee',
            )
        ]
        verbose_name = 'Исполнитель задачи протокола'
        verbose_name_plural = 'Исполнители задач протоколов'

    def __str__(self):
        return f'{self.action}: {self.user}'


class ProtocolHistoryEvent(models.Model):
    """The protocol's own audit trail.

    `revision` is recorded on the event because the same protocol can be sent
    for approval, returned and resent: the history has to say *which* revision
    a signature or a return belongs to.
    """

    class EventType(models.TextChoices):
        CREATED = 'CREATED', 'Протокол создан'
        EDITED = 'EDITED', 'Протокол отредактирован'
        SENT_FOR_APPROVAL = 'SENT_FOR_APPROVAL', 'Протокол отправлен на согласование'
        APPROVED_BY_USER = 'APPROVED_BY_USER', 'Участник согласовал протокол'
        RETURNED_FOR_REVISION = 'RETURNED_FOR_REVISION', 'Протокол возвращён на доработку'
        RESENT_FOR_APPROVAL = (
            'RESENT_FOR_APPROVAL',
            'Протокол повторно отправлен на согласование',
        )
        ARCHIVED = 'ARCHIVED', 'Протокол помещён в архив'
        TASKS_CREATED = 'TASKS_CREATED', 'Задачи созданы'

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='history_events',
        verbose_name='Протокол',
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='protocol_history_events',
        verbose_name='Пользователь',
        blank=True,
        null=True,
    )
    event_type = models.CharField('Тип события', max_length=40, choices=EventType.choices)
    revision = models.PositiveIntegerField('Редакция', default=0)
    message = models.TextField('Сообщение')
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Событие истории протокола'
        verbose_name_plural = 'События истории протоколов'

    def __str__(self):
        return f'{self.protocol}: {self.get_event_type_display()}'
