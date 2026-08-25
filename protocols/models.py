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

from uuid import uuid4

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


def protocol_attachment_upload_to(instance, filename):
    """`protocols/attachments/<protocol_id>/<uuid>.<ext>` — never the real name.

    The stored path carries no user-supplied text at all: the browser's name is
    kept in `original_name` for the download, and the file on disk is a UUID, so
    a crafted filename cannot escape the directory or collide with another
    upload. Deliberately a separate tree from `acts/attachments/`.
    """
    parts = (filename or '').rsplit('.', 1)
    extension = f'.{parts[1].lower()}' if len(parts) == 2 else ''
    protocol_id = instance.protocol_id or 'unassigned'
    return f'protocols/attachments/{protocol_id}/{uuid4().hex}{extension}'


class ProtocolAction(models.Model):
    """A decision recorded in the protocol as a future task.

    Not a `tasks.Task` and not linked to one: real tasks are created by a later
    stage from an archived protocol, and until then this table carries the
    wording, the department and the deadline on its own.

    One decision stays one row however its execution is organised.
    `split_for_assignees` changes only *how many* `tasks.Task` rows archiving
    produces — one shared task for everybody, or one independent task each — and
    never who must approve the protocol, how the official document reads, or how
    many decisions the protocol has.
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
    # Execution only, and meaningless below two assignees: `_apply_actions()`
    # normalizes it back to False for a single assignee, so «split» never
    # describes a decision that has nobody to split it between. `default=False`
    # is also what keeps every row stored before this field existed in the
    # shared-task mode it was created under.
    split_for_assignees = models.BooleanField(
        'Разбить задачу для участников', default=False
    )
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
        # Collaboration. The history records that something was added or
        # removed and by whom; the text of a comment and the contents of a
        # file stay in their own tables, which the «Вложения и комментарии»
        # tab reads. A return keeps its own `RETURNED_FOR_REVISION` event and
        # deliberately does not add a `COMMENT_ADDED` one beside it.
        COMMENT_ADDED = 'COMMENT_ADDED', 'Добавлен комментарий'
        ATTACHMENT_ADDED = 'ATTACHMENT_ADDED', 'Добавлено вложение'
        ATTACHMENT_DELETED = 'ATTACHMENT_DELETED', 'Вложение удалено'

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


class ProtocolApproval(models.Model):
    """One person's decision on one revision of one protocol.

    A row per `(protocol, revision, user)`, created when that revision is sent
    for approval and never deleted afterwards: a resubmission opens a *new*
    revision with its own rows, so revision 1's signatures stay readable next
    to revision 2's. The two `required_as_*` flags say why the person had to
    sign — a participant marked «требуется согласование», an assignee of a
    protocol task, or both — and remain the historical answer even after the
    protocol is edited.

    `display_name`, `position` and `department_name` are frozen at submission
    for the same reason participant snapshots are: the archive must keep saying
    who signed and in which role, whatever the profile does later.

    `task` is a lazy `'tasks.Task'` reference on purpose: `tasks.models` already
    imports `protocols.models`, and a real import here would close the circle.
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Ожидает согласования'
        APPROVED = 'APPROVED', 'Согласовано'
        RETURNED = 'RETURNED', 'Возвращено на доработку'
        CANCELLED = 'CANCELLED', 'Отменено'

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='approvals',
        verbose_name='Протокол',
    )
    revision = models.PositiveIntegerField('Редакция')
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='protocol_approvals',
        verbose_name='Согласующий',
    )
    status = models.CharField(
        'Статус',
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    task = models.OneToOneField(
        'tasks.Task',
        on_delete=models.SET_NULL,
        related_name='protocol_approval',
        blank=True,
        null=True,
        verbose_name='Задача согласования',
    )
    required_as_participant = models.BooleanField(
        'Согласует как участник', default=False
    )
    required_as_action_assignee = models.BooleanField(
        'Согласует как исполнитель задачи', default=False
    )
    display_name = models.CharField('ФИО в документе', max_length=180)
    position = models.CharField('Должность в документе', max_length=120, blank=True)
    department_name = models.CharField('Подразделение в документе', max_length=120, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    decided_at = models.DateTimeField('Решение принято', blank=True, null=True)
    return_comment = models.TextField('Комментарий возврата', blank=True)

    class Meta:
        ordering = ['-revision', 'pk']
        verbose_name = 'Согласование протокола'
        verbose_name_plural = 'Согласования протоколов'
        constraints = [
            models.UniqueConstraint(
                fields=['protocol', 'revision', 'user'],
                name='unique_protocol_approval_per_revision',
            )
        ]

    def __str__(self):
        return f'{self.protocol} ред. {self.revision}: {self.display_name}'

    @property
    def is_pending(self):
        return self.status == self.Status.PENDING


class ProtocolComment(models.Model):
    """One message in the protocol's collaboration feed.

    A real foreign key to `Protocol`, not a generic relation: a comment about a
    protocol is not a comment about an act, and the two feeds share nothing but
    their shape. `author` is nullable and `SET_NULL` for the same reason
    `ActComment`'s is — a deleted account must not take the discussion with it.

    The mandatory «вернуть на доработку» reason is stored here as well, in the
    same transaction as the return, so the feed reads as the conversation it
    is. That copy never replaces `ProtocolApproval.return_comment`, which stays
    the authoritative record of the decision.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='comments',
        verbose_name='Протокол',
    )
    author = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='protocol_comments',
        verbose_name='Автор',
        blank=True,
        null=True,
    )
    text = models.TextField('Комментарий')
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Комментарий к протоколу'
        verbose_name_plural = 'Комментарии к протоколам'

    def __str__(self):
        author = self.author.get_username() if self.author else 'без автора'
        return f'{self.protocol}: {author}'


class ProtocolAttachment(models.Model):
    """One file attached to a protocol.

    Its own table and its own `protocols/attachments/` tree — never
    `ActAttachment` and never a generic relation. `file` is served only through
    `protocols:download_attachment`, which re-checks who may read the protocol;
    nothing here is reachable from a public media URL in production.

    `original_name`, `file_size` and `content_type` are copied at upload so the
    card and the download work without touching storage, and so a file that
    later disappears from disk is still an identifiable row rather than a 500.
    """

    protocol = models.ForeignKey(
        Protocol,
        on_delete=models.CASCADE,
        related_name='attachments',
        verbose_name='Протокол',
    )
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='protocol_attachments',
        verbose_name='Загрузил',
        blank=True,
        null=True,
    )
    file = models.FileField('Файл', upload_to=protocol_attachment_upload_to)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    description = models.TextField('Описание', blank=True)
    file_size = models.PositiveIntegerField('Размер файла', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    uploaded_at = models.DateTimeField('Загружено', auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at', '-pk']
        verbose_name = 'Вложение протокола'
        verbose_name_plural = 'Вложения протоколов'

    def __str__(self):
        return f'{self.protocol}: {self.original_name}'
