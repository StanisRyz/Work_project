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

    It has no status and no workflow: the record is written once and the work
    it creates lives in «Задачи», where it is tracked like every other task.
    The pk is the number people see — no separate series is allocated, because
    nothing here is numbered per type the way a protocol is.
    """

    class Origin(models.TextChoices):
        EXTERNAL_AUDIT = 'EXTERNAL_AUDIT', 'Внешний аудит'
        INTERNAL_AUDIT = 'INTERNAL_AUDIT', 'Внутренний аудит'

    origin = models.CharField(
        'Источник', max_length=32, choices=Origin.choices,
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
