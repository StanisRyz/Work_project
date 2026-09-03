"""What an СМК corrective action came out of.

Отдел СМК records the outcome of an audit: where it came from, which
non-conformities it found, and what has to be done about each of them. The
first two are stored here and nowhere else; the third becomes a real
`tasks.Task`, exactly as a protocol decision does — this app owns the source
document, never a second task system.

`SmkCorrectiveAction` is therefore deliberately *not* a `tasks.Task`: it is the
measure as it is written down in the СМК record, and the task it produces is
created once by `smk/services.py` and linked back through `Task.smk_action`.
Nothing here writes itself — every mutation goes through the service.
"""

from django.contrib.auth.models import User
from django.db import models

from accounts.models import Department


class SmkSource(models.Model):
    """One СМК record: an audit, its findings and the measures it produced.

    It has no workflow: the record is written once and the work it creates
    lives in «Задачи», where it is tracked like every other task. Its only
    state is where it is filed — «В работе» or «Архив» — and that moves solely
    because somebody pressed «Архивировать», never because a task closed.
    The pk is the number people see — no separate series is allocated, because
    nothing here is numbered per type the way a protocol is.
    """

    class Origin(models.TextChoices):
        EXTERNAL_AUDIT = 'EXTERNAL_AUDIT', 'Внешний аудит'
        INTERNAL_AUDIT = 'INTERNAL_AUDIT', 'Внутренний аудит'

    class Status(models.TextChoices):
        """Where the record is read, and nothing more.

        Two values on purpose: this is a shelf, not a workflow. A record stays
        `ACTIVE` until somebody archives it by hand — completing its tasks
        never moves it, because the tasks are tracked in «Задачи» and the
        record is the document they came out of.
        """

        ACTIVE = 'ACTIVE', 'В работе'
        ARCHIVED = 'ARCHIVED', 'Архив'

    origin = models.CharField(
        'Источник', max_length=32, choices=Origin.choices,
    )
    status = models.CharField(
        'Статус', max_length=16, choices=Status.choices, default=Status.ACTIVE,
    )
    # When the audit actually happened — the author's own answer, and never
    # `created_at`. The two are deliberately separate: a record is often
    # written up days after the audit it describes, and the page shows this
    # one. Nullable only so the column could be added to rows stored before it
    # existed; the form has required it ever since.
    audit_date = models.DateField('Дата аудита', null=True, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='created_smk_sources',
        verbose_name='Создал',
    )
    created_at = models.DateTimeField('Создан', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)
    # Who put the record on the shelf and when. Kept next to `status` rather
    # than in a history table: there is exactly one transition and it happens
    # at most once, so a timeline of it would hold a single row.
    archived_at = models.DateTimeField('Архивирован', null=True, blank=True)
    archived_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='archived_smk_sources',
        null=True,
        blank=True,
        verbose_name='Архивировал',
    )

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Запись СМК'
        verbose_name_plural = 'Записи СМК'

    def __str__(self):
        return f'СМК №{self.pk}'

    @property
    def label(self):
        """The identifier a person recognises, in one place.

        Read by `tasks.presentation` for the registry's «Источник» column, so
        the record and the task row can never name it differently.
        """
        return f'СМК №{self.pk}'

    @property
    def is_archived(self):
        """Read by the template and the permission check, so «архивирована»
        is spelled out once and never as a status comparison in markup."""
        return self.status == self.Status.ARCHIVED


class SmkHistoryEvent(models.Model):
    """The record's own audit trail, in the shape acts and protocols already use.

    Deliberately thin: an СМК record is written once and has a single
    transition, so the trail is a short list of facts — «создана», one line per
    задача the measures produced, and «в архив». There are no fragments, no
    filters and no editing: `smk/services.py` is the only writer, inside the
    same `atomic()` block as the change it describes, so an event without its
    change (or the other way round) cannot be stored.
    """

    class EventType(models.TextChoices):
        CREATED = 'CREATED', 'Запись СМК создана'
        TASK_CREATED = 'TASK_CREATED', 'Задача по мероприятию создана'
        ARCHIVED = 'ARCHIVED', 'Запись СМК помещена в архив'
        # Written by nothing today: the record is immutable by design, and the
        # only field that moves is `status`, which has its own event above. It
        # is named here so an edit path, if one is ever added, records the fact
        # rather than inventing a type for it.
        EDITED = 'EDITED', 'Запись СМК отредактирована'

    source = models.ForeignKey(
        SmkSource,
        on_delete=models.CASCADE,
        related_name='history_events',
        verbose_name='Запись СМК',
    )
    # `SET_NULL`, like the act and protocol trails: a deleted account must not
    # take the history of what it did with it.
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='smk_history_events',
        blank=True,
        null=True,
        verbose_name='Пользователь',
    )
    event_type = models.CharField('Тип события', max_length=40, choices=EventType.choices)
    message = models.TextField('Сообщение')
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Событие истории СМК'
        verbose_name_plural = 'События истории СМК'

    def __str__(self):
        return f'{self.source}: {self.get_event_type_display()}'


class SmkNonConformity(models.Model):
    """One несоответствие found by the audit.

    Plain text and an order, nothing else: it is evidence, not work. What is
    done about it is a `SmkCorrectiveAction`, and the two are deliberately not
    linked row to row — one measure often answers several findings, and the
    record is read as a whole.
    """

    source = models.ForeignKey(
        SmkSource,
        on_delete=models.CASCADE,
        related_name='non_conformities',
        verbose_name='Запись СМК',
    )
    text = models.TextField('Выявленное несоответствие')
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Выявленное несоответствие'
        verbose_name_plural = 'Выявленные несоответствия'

    def __str__(self):
        return f'{self.source}: {self.text[:60]}'

    @property
    def number(self):
        """«№1» as the page shows it — the stored order, not the primary key."""
        return self.display_order + 1


class SmkCorrectiveAction(models.Model):
    """One корректирующее мероприятие, and the task it becomes.

    The wording, the department and the deadline are carried here; the real
    task is created from them by `smk.services.create_smk_source()` in the same
    transaction, and `Task.smk_action` is the only link between the two. At
    most one task per measure — a database constraint on `Task`, not a check
    here.
    """

    source = models.ForeignKey(
        SmkSource,
        on_delete=models.CASCADE,
        related_name='actions',
        verbose_name='Запись СМК',
    )
    task_text = models.TextField('Корректирующее мероприятие')
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='smk_actions',
        verbose_name='Подразделение',
    )
    due_date = models.DateField('Срок')
    # Which finding this measure answers, when the author said so. Optional and
    # `SET_NULL` on purpose: one measure often answers several findings at once,
    # so the link is a statement the author may make rather than a rule the
    # record must satisfy, and a measure never disappears with a finding.
    non_conformity = models.ForeignKey(
        'SmkNonConformity',
        on_delete=models.SET_NULL,
        related_name='corrective_actions',
        null=True,
        blank=True,
        verbose_name='Выявленное несоответствие',
    )
    # Whether the real task this measure becomes may only be completed with a
    # file attached. Stored on the measure because the requirement is the
    # author's decision; the task copies it once, at creation, and never reads
    # it back — `Task.requires_attachment` is the authority from then on, and
    # `tasks.services.complete_task()` is the only place it is enforced.
    # `default=False` keeps every row stored before this field existed exactly
    # as permissive as it was.
    requires_attachment = models.BooleanField('Обязательно вложение', default=False)
    display_order = models.PositiveIntegerField('Порядок отображения', default=0)

    class Meta:
        ordering = ['display_order', 'pk']
        verbose_name = 'Корректирующее мероприятие'
        verbose_name_plural = 'Корректирующие мероприятия'

    def __str__(self):
        return f'{self.source}: {self.task_text[:60]}'


class SmkActionAssignee(models.Model):
    """Who the measure is written on.

    The same shape as `ProtocolActionAssignee`: the draft keeps its own
    assignee list, and `TaskAssignee` is what the real task is completed
    against.
    """

    action = models.ForeignKey(
        SmkCorrectiveAction,
        on_delete=models.CASCADE,
        related_name='assignees',
        verbose_name='Корректирующее мероприятие',
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='smk_action_assignments',
        verbose_name='Исполнитель',
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=['action', 'user'],
                name='unique_smk_action_assignee',
            )
        ]
        verbose_name = 'Исполнитель мероприятия СМК'
        verbose_name_plural = 'Исполнители мероприятий СМК'

    def __str__(self):
        return f'{self.action}: {self.user}'
