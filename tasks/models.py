"""Tasks, and where a task came from.

A `Task` is the shared unit of work employees see in «Задачи». It is created
by a workflow service — never by hand — and `source_type` is the single
authoritative answer to *what produced it*. The nullable source relations
below exist so more than one origin can be represented; they are never the
thing domain logic branches on, because a nullable relation cannot tell an
absent origin from a wrong one.

Exactly one shape is valid per source type, enforced both by `clean()` (with
readable messages) and by a database check constraint (the last line of
defence). Two of those shapes come in a shared and a split variant, told apart
by `individual_assignee`: NULL for the one task everybody shares, set for the
one task split off for that person.

Two of the four source types are *routing* entries rather than work:
`PROTOCOL_APPROVAL` and `ACT_WORKFLOW`. Neither is completed with an
execution comment — their real action is taken on the source document, and
the document's workflow service closes them.
"""

from pathlib import Path
from uuid import uuid4

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
        # A routing entry for the stage an act is currently waiting on, not a
        # corrective action. Deliberately a separate type from `ACT`, which
        # requires the act → root analysis → corrective action chain and is
        # completed by an employee; this one is opened and closed by
        # `acts/services.py` as the act moves.
        ACT_WORKFLOW = 'ACT_WORKFLOW', 'Этап обработки акта'
        # Real, executable work for ПДО: products the КО decision prohibited
        # from use have to be re-planned. Unlike `ACT_WORKFLOW` it is completed
        # by the employee in the ordinary way, and unlike `ACT` it comes from
        # the КО decision rather than from the ТО analysis, so it has no
        # corrective-action chain to hang on.
        ACT_REJECTION = 'ACT_REJECTION', 'Брак по акту'

    class WorkflowStage(models.TextChoices):
        """Which act stage an `ACT_WORKFLOW` task represents.

        Persisted rather than read back off `Act.status`: a closed task must
        keep saying what it was for, and the act has long moved on by then.
        """

        KO_REVIEW = 'KO_REVIEW', 'Рассмотрение КО'
        TO_ANALYSIS = 'TO_ANALYSIS', 'Анализ ТО'
        OTK_REVIEW = 'OTK_REVIEW', 'Итоговая проверка ОТК'
        OTK_REWORK = 'OTK_REWORK', 'Доработка ОТК'

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
    # A foreign key rather than a one-to-one, for the same reason
    # `protocol_action` is one: a corrective action marked
    # `split_for_assignees` produces one independent task per assignee. How
    # many tasks it may own is stated by the two unique constraints below, not
    # by the relation.
    source_action = models.ForeignKey(
        ActCorrectiveAction,
        on_delete=models.PROTECT,
        related_name='tasks',
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
    # Which single assignee this task was split off for — of `source_action`
    # for an act task, of `protocol_action` for a protocol one — and NULL for a
    # shared task. The one field that tells the two modes apart, and the same
    # field for both domains rather than one each. It is not the assignee list:
    # `TaskAssignee` still owns that, and for a split task holds exactly this
    # same user.
    individual_assignee = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='individual_protocol_tasks',
        null=True,
        blank=True,
        verbose_name='Персональный исполнитель',
    )
    # Filled for `ACT_WORKFLOW` only, and blank for every other source. It is
    # the historical meaning of the row: which stage of the act's route this
    # queue entry stood for when it was opened.
    workflow_stage = models.CharField(
        'Этап маршрута акта',
        max_length=32,
        choices=WorkflowStage.choices,
        blank=True,
    )
    task_text = models.TextField('Задача')
    # Required for every real work item and enforced as such by the source
    # constraint below. Nullable only because an `ACT_WORKFLOW` entry
    # belongs to a *role* — every active КО, ТО or ОТК employee — and a
    # role has no single department to name.
    department = models.ForeignKey(
        Department, on_delete=models.PROTECT, null=True, blank=True,
        verbose_name='Подразделение',
    )
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
    # A snapshot, taken from `ProtocolAction.requires_attachment` or
    # `ActCorrectiveAction.requires_attachment` when the task is created, of
    # whether finishing this work needs at least one `TaskAttachment`.
    # Deliberately copied rather than read through the relation: the source row
    # stays editable until its task exists, and a completed task must keep
    # saying what was required of it. `complete_task()` is the authority.
    # Routing entries (`PROTOCOL_APPROVAL`, `ACT_WORKFLOW`) and `ACT_REJECTION`
    # are never given the flag; `default=False` is what every row stored before
    # this field existed carries.
    requires_attachment = models.BooleanField('Требуется вложение', default=False)

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
                    # `individual_assignee` is left free here and in the
                    # `PROTOCOL_ACTION` branch below: NULL is a shared task,
                    # set is one split off for that person, and both are valid
                    # shapes of the same source. Only a routing entry, which
                    # nobody splits, still forbids it outright.
                    #
                    # `department__isnull=False` and `workflow_stage=''` are
                    # stated on all three existing branches, so making the
                    # column nullable and adding the stage for `ACT_WORKFLOW`
                    # relaxes nothing for the source types that already exist.
                    Q(
                        source_type='ACT',
                        act__isnull=False,
                        root_analysis__isnull=False,
                        source_action__isnull=False,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | Q(
                        source_type='PROTOCOL_APPROVAL',
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    | Q(
                        source_type='PROTOCOL_ACTION',
                        act__isnull=True,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=False,
                        protocol_action__isnull=False,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    # Real ПДО work created by a КО «запретить использование»:
                    # the act and a department, but no corrective-action
                    # chain — it is not a ТО mitigation — no protocol, no
                    # stage, and nobody to split it between.
                    | Q(
                        source_type='ACT_REJECTION',
                        act__isnull=False,
                        root_analysis__isnull=True,
                        source_action__isnull=True,
                        protocol__isnull=True,
                        protocol_action__isnull=True,
                        individual_assignee__isnull=True,
                        department__isnull=False,
                        workflow_stage='',
                    )
                    # The routing entry for an act stage: the act alone, no
                    # corrective-action chain, no protocol, nobody to split it
                    # between — and a stage that must actually be named.
                    | (
                        Q(
                            source_type='ACT_WORKFLOW',
                            act__isnull=False,
                            root_analysis__isnull=True,
                            source_action__isnull=True,
                            protocol__isnull=True,
                            protocol_action__isnull=True,
                            individual_assignee__isnull=True,
                        )
                        # `workflow_stage` is a `CharField`, so "must be
                        # filled" is stated as "is not the empty string".
                        & ~Q(workflow_stage='')
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
            # The same pair for act tasks, replacing what `source_action`'s
            # dropped one-to-one used to guarantee: at most one task everybody
            # shares, and at most one per assignee within the split. NULL
            # `source_action` rows are all distinct to a unique index, so no
            # protocol task is affected by either.
            models.UniqueConstraint(
                fields=['source_action'],
                condition=Q(individual_assignee__isnull=True),
                name='unique_shared_act_action_task',
            ),
            models.UniqueConstraint(
                fields=['source_action', 'individual_assignee'],
                name='unique_individual_act_action_task',
            ),
            # One rejection task per act, stated by the database rather than by
            # a service check: a retried or concurrent КО transition must not
            # be able to hand ПДО the same notice twice, whatever the service
            # looked up a moment earlier. Rows of every other source type have
            # a `source_type` that fails the condition, so none is affected.
            models.UniqueConstraint(
                fields=['act'],
                condition=Q(source_type='ACT_REJECTION'),
                name='unique_act_rejection_task',
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

    @property
    def is_act_workflow_task(self):
        return self.source_type == self.SourceType.ACT_WORKFLOW

    @property
    def is_act_rejection_task(self):
        return self.source_type == self.SourceType.ACT_REJECTION

    @property
    def is_routing_task(self):
        """A queue entry whose real action happens on the source document.

        The one answer the completion guard, the task page and the attachment
        card all ask: a routing task has no execution form, is never completed
        with a comment and carries no attachments.
        """
        return self.source_type in {
            self.SourceType.PROTOCOL_APPROVAL,
            self.SourceType.ACT_WORKFLOW,
        }

    def clean(self):
        """Readable source validation, including the rules SQL cannot express.

        The shape rules deliberately restate the check constraint: a service or
        a form gets a field error instead of an `IntegrityError`. Every rule
        that spans two tables lives *only* here, because no single-row check
        constraint can state one: that the source relations of an act task
        agree with each other, that a protocol task's decision belongs to its
        protocol, and that a split task's individual assignee really is an
        assignee of the source it was split off from.

        `individual_assignee` is optional for both split-capable sources — `ACT`
        and `PROTOCOL_ACTION` — and forbidden on an approval task, which is one
        person's queue entry and has nothing to split.
        """
        super().clean()
        required, forbidden = {
            self.SourceType.ACT: (
                ('act', 'root_analysis', 'source_action'),
                ('protocol', 'protocol_action'),
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
            self.SourceType.ACT_WORKFLOW: (
                ('act',),
                ('root_analysis', 'source_action', 'protocol', 'protocol_action',
                 'individual_assignee'),
            ),
            self.SourceType.ACT_REJECTION: (
                ('act',),
                ('root_analysis', 'source_action', 'protocol', 'protocol_action',
                 'individual_assignee'),
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
        # The act source relations must describe one chain, not three
        # independent rows: the corrective action's root analysis is the task's
        # root analysis, and that analysis belongs to the task's act.
        if self.source_action_id is not None and self.root_analysis_id is not None:
            if self.source_action.root_analysis_id != self.root_analysis_id:
                errors['source_action'] = (
                    'Корректирующее мероприятие должно относиться к указанной корневой проработке.'
                )
        if self.root_analysis_id is not None and self.act_id is not None:
            if self.root_analysis.act_id != self.act_id:
                errors['root_analysis'] = (
                    'Корневая проработка должна относиться к тому же акту.'
                )
        if self.individual_assignee_id is not None:
            if self.protocol_action_id is not None and not self.protocol_action.assignees.filter(
                user_id=self.individual_assignee_id
            ).exists():
                errors['individual_assignee'] = (
                    'Персональная задача создаётся только на исполнителя этой задачи протокола.'
                )
            if self.source_action_id is not None and not self.source_action.assignees.filter(
                user_id=self.individual_assignee_id
            ).exists():
                errors['individual_assignee'] = (
                    'Персональная задача создаётся только на исполнителя этого '
                    'корректирующего мероприятия.'
                )
        # The stage is what an `ACT_WORKFLOW` row *means*, and it is
        # meaningless anywhere else; `department` is required by every real
        # work item and unavailable for a role-wide routing entry.
        if self.source_type == self.SourceType.ACT_WORKFLOW:
            if not self.workflow_stage:
                errors['workflow_stage'] = f'Обязательно для источника «{source_name}».'
        else:
            if self.workflow_stage:
                errors['workflow_stage'] = f'Недопустимо для источника «{source_name}».'
            if required and self.department_id is None:
                errors['department'] = f'Обязательно для источника «{source_name}».'
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


def task_attachment_upload_to(instance, filename):
    """`tasks/attachments/<task_id>/<uuid>.<ext>` — never the browser's name.

    The same shape act and protocol attachments use: a UUID file name means an
    uploaded name can neither collide, escape its directory, nor become a
    guessable URL. `MEDIA_ROOT` is not published by the web server, so the file
    is only ever reachable through the permission-checked download view.
    """
    extension = Path(filename).suffix.lower()
    task_id = instance.task_id or 'unassigned'
    return f'tasks/attachments/{task_id}/{uuid4().hex}{extension}'


class TaskAttachment(models.Model):
    """An optional file attached to an ordinary, executable task.

    Optional is the whole point: a task is completed with its execution
    comment and zero attachments, exactly as before. Uploading is a separate
    request from completing, so no file can ever become a precondition of
    finishing the work.

    Routing tasks (`PROTOCOL_APPROVAL`, `ACT_WORKFLOW`) carry no attachments —
    their real action happens on the source document, which has attachments of
    its own. `tasks.permissions` states that rule; the model only stores.
    """

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Задача',
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='task_attachments',
        verbose_name='Загрузил',
        blank=True,
        null=True,
    )
    file = models.FileField('Файл', upload_to=task_attachment_upload_to)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    file_size = models.PositiveIntegerField('Размер файла', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    created_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Вложение задачи'
        verbose_name_plural = 'Вложения задач'

    def __str__(self):
        return f'Задача #{self.task_id}: {self.original_name}'
