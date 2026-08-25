"""Tasks, and where a task came from.

A `Task` is the shared unit of work employees see in «Задачи». It is created
by a workflow service — never by hand — and `source_type` is the single
authoritative answer to *what produced it*. The nullable source relations
below exist so more than one origin can be represented; they are never the
thing domain logic branches on, because a nullable relation cannot tell an
absent origin from a wrong one.

Exactly one shape is valid per source type, enforced both by `clean()` (with
readable messages) and by a database check constraint (the last line of
defence). Only `ACT` tasks are produced by production code today; the protocol
shapes are structural preparation, and nothing creates them yet.
"""

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from accounts.models import Department
from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
from protocols.models import Protocol, ProtocolAction
from references.models import TaskStatus


class Task(models.Model):
    class SourceType(models.TextChoices):
        ACT = 'ACT', 'По акту'
        PROTOCOL_APPROVAL = 'PROTOCOL_APPROVAL', 'Согласование протокола'
        PROTOCOL_ACTION = 'PROTOCOL_ACTION', 'По протоколу'

    # `default` is what let the existing production rows take a value when the
    # column was added, and it keeps a source type from ever being NULL. It is
    # not a licence to omit the field: every service states it explicitly, and
    # a protocol-shaped task left on the default is refused by the constraint
    # below rather than silently stored as an act task.
    source_type = models.CharField(
        'Тип источника',
        max_length=32,
        choices=SourceType.choices,
        default=SourceType.ACT,
    )
    source_action = models.OneToOneField(
        ActCorrectiveAction,
        on_delete=models.PROTECT,
        related_name='task',
        null=True,
        blank=True,
        verbose_name='Исходное корректирующее мероприятие',
    )
    act = models.ForeignKey(
        Act, on_delete=models.PROTECT, related_name='tasks',
        null=True, blank=True, verbose_name='Акт',
    )
    root_analysis = models.ForeignKey(
        ActRootAnalysis,
        on_delete=models.PROTECT,
        related_name='tasks',
        null=True,
        blank=True,
        verbose_name='Корневая причина',
    )
    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.PROTECT,
        related_name='tasks',
        null=True,
        blank=True,
        verbose_name='Протокол',
    )
    # A foreign key rather than a one-to-one, because a decision marked
    # `split_for_assignees` produces one independent task per assignee. How
    # many tasks a decision may own is therefore not left to the relation: the
    # two unique constraints below say it exactly — at most one shared task,
    # and at most one task per individual assignee.
    protocol_action = models.ForeignKey(
        ProtocolAction,
        on_delete=models.PROTECT,
        related_name='tasks',
        null=True,
        blank=True,
        verbose_name='Задача протокола',
    )
    # Which single assignee of `protocol_action` this task was split off for,
    # and NULL for a shared task — the one field that tells the two modes
    # apart. It is not the assignee list: `TaskAssignee` still owns that, and
    # for a split task holds exactly this same user.
    individual_assignee = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='individual_protocol_tasks',
        null=True,
        blank=True,
        verbose_name='Персональный исполнитель',
    )
    task_text = models.TextField('Задача')
    department = models.ForeignKey(Department, on_delete=models.PROTECT, verbose_name='Подразделение')
    due_date = models.DateField('Срок')
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='created_tasks', verbose_name='Создал')
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    # Bumped by any save of the task row itself, which is what lets the
    # real-time sync service build a cheap revision token for tasks.
    updated_at = models.DateTimeField('Обновлена', auto_now=True)
    status = models.ForeignKey(TaskStatus, on_delete=models.PROTECT, verbose_name='Статус')
    completed_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='completed_tasks', verbose_name='Завершил',
    )
    completed_at = models.DateTimeField('Завершена', null=True, blank=True)
    execution_comment = models.TextField('Результат выполнения', blank=True)

    class Meta:
        ordering = ['due_date', 'created_at']
        verbose_name = 'Задача'
        verbose_name_plural = 'Задачи'
        constraints = [
            # One constraint listing all three valid shapes, rather than one
            # per type: written this way an unknown `source_type` matches no
            # branch and is rejected too, so the table can never hold a mixed
            # or half-filled source.
            models.CheckConstraint(
                condition=(
                    Q(
                        source_type='ACT',
                        act__isnull=False,
                        root_analysis__isnull=False,
                        source_action__isnull=False,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                    )
                    | Q(
                        source_type='PROTOCOL_APPROVAL',
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                    )
                    # The only branch that leaves `individual_assignee` free:
                    # NULL is a shared task, set is one split off for that
                    # person. Both are valid shapes of the same source.
                    | Q(
                        source_type='PROTOCOL_ACTION',
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=False,
                    )
                ),
                name='task_source_relations_match_source_type',
            ),
            # What the dropped one-to-one used to guarantee, now stated for the
            # shared mode alone: a decision has at most one task everybody
            # shares. Rows with a NULL `protocol_action` are all distinct to a
            # unique index, so no act task is affected.
            models.UniqueConstraint(
                fields=['protocol_action'],
                condition=Q(individual_assignee__isnull=True),
                name='unique_shared_protocol_action_task',
            ),
            # And the split mode: one task per person, so a repeated or
            # concurrent finalization cannot hand the same assignee a second
            # copy. NULLs are distinct here too, which is why the shared rule
            # above needs its own constraint.
            models.UniqueConstraint(
                fields=['protocol_action', 'individual_assignee'],
                name='unique_individual_protocol_action_task',
            ),
        ]

    def __str__(self):
        return f'Задача #{self.pk}: {self.task_text[:60]}'

    @property
    def is_act_task(self):
        return self.source_type == self.SourceType.ACT

    @property
    def is_protocol_approval_task(self):
        return self.source_type == self.SourceType.PROTOCOL_APPROVAL

    def clean(self):
        """Readable source validation, including the rules SQL cannot express.

        The shape rules deliberately restate the check constraint: a service or
        a form gets a field error instead of an `IntegrityError`. The two
        cross-table rules live *only* here — the `protocol_action`/`protocol`
        agreement and, for a split task, the individual assignee really being
        an assignee of that decision. Neither spans one row, so no check
        constraint can state them safely.

        `individual_assignee` is optional for `PROTOCOL_ACTION` and forbidden
        everywhere else: it is what tells a split task from a shared one, and
        an act task has no decision to be split off from.
        """
        super().clean()
        required, forbidden = {
            self.SourceType.ACT: (
                ('act', 'root_analysis', 'source_action'),
                ('protocol', 'protocol_action', 'individual_assignee'),
            ),
            self.SourceType.PROTOCOL_APPROVAL: (
                ('protocol',),
                ('act', 'root_analysis', 'source_action', 'protocol_action',
                 'individual_assignee'),
            ),
            self.SourceType.PROTOCOL_ACTION: (
                ('protocol', 'protocol_action'),
                ('act', 'root_analysis', 'source_action'),
            ),
        }.get(self.source_type, ((), ()))
        if not required:
            raise ValidationError({'source_type': 'Неизвестный тип источника задачи.'})
        errors = {}
        source_name = self.get_source_type_display()
        for name in required:
            if getattr(self, f'{name}_id') is None:
                errors[name] = f'Обязательно для источника «{source_name}».'
        for name in forbidden:
            if getattr(self, f'{name}_id') is not None:
                errors[name] = f'Недопустимо для источника «{source_name}».'
        if (
            self.protocol_action_id is not None
            and self.protocol_id is not None
            and self.protocol_action.protocol_id != self.protocol_id
        ):
            errors['protocol_action'] = 'Задача протокола должна принадлежать тому же протоколу.'
        if (
            self.individual_assignee_id is not None
            and self.protocol_action_id is not None
            and not self.protocol_action.assignees.filter(
                user_id=self.individual_assignee_id
            ).exists()
        ):
            errors['individual_assignee'] = (
                'Персональная задача создаётся только на исполнителя этой задачи протокола.'
            )
        if errors:
            raise ValidationError(errors)


class TaskAssignee(models.Model):
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='assignees', verbose_name='Задача')
    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name='task_assignments', verbose_name='Исполнитель')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['task', 'user'], name='unique_task_assignee')]
        verbose_name = 'Исполнитель задачи'
        verbose_name_plural = 'Исполнители задач'

    def __str__(self):
        return f'{self.task}: {self.user}'
