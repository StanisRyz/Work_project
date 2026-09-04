"""In-app notifications and their outbound delivery queue.

A `Notification` is created only by `notifications/services.py`, and
`source_type` is the single authoritative answer to *what it is about*. The
three source relations below are nullable so more than one origin can be
represented; they are never what business logic branches on, because a
nullable relation cannot tell an absent source from a wrong one.

Exactly one shape is valid per source type, enforced by `clean()` (readable
messages) and by a database check constraint (the last line of defence).
`related_act` keeps its name and its meaning — every notification that existed
before protocols is an `ACT` one and is untouched.

`BUG` is the one source that is not a quality document: it points at a
`bugs.BugReport`, the message somebody typed into «Сообщить об ошибке». It
travels the very same pipeline as the rest — deduplication, the bell, the email
queue — because a second notification system is exactly what this app exists to
prevent.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone


class Notification(models.Model):
    class SourceType(models.TextChoices):
        ACT = 'ACT', 'Акт'
        PROTOCOL = 'PROTOCOL', 'Протокол'
        TASK = 'TASK', 'Задача'
        BUG = 'BUG', 'Сообщение об ошибке'

    class EventType(models.TextChoices):
        ACT_SENT_TO_KO = 'ACT_SENT_TO_KO', 'Акт передан в КО'
        ACT_SENT_TO_TO = 'ACT_SENT_TO_TO', 'Акт передан в ТО'
        ACT_SENT_TO_OTK = 'ACT_SENT_TO_OTK', 'Акт передан на проверку ОТК'
        ACT_RETURNED_TO_OTK = 'ACT_RETURNED_TO_OTK', 'Акт возвращён в ОТК'
        ACT_RETURNED_TO_KO = 'ACT_RETURNED_TO_KO', 'Акт возвращён в КО'
        ACT_RETURNED_TO_TO = 'ACT_RETURNED_TO_TO', 'Акт возвращён в ТО'
        ACTION_ASSIGNED = 'ACTION_ASSIGNED', 'Назначено мероприятие'
        ACT_APPROVED = 'ACT_APPROVED', 'Акт утверждён'
        COMMENT_ADDED = 'COMMENT_ADDED', 'Добавлен комментарий'
        PROTOCOL_APPROVAL_REQUIRED = (
            'PROTOCOL_APPROVAL_REQUIRED', 'Требуется согласование протокола'
        )
        PROTOCOL_RETURNED_FOR_REVISION = (
            'PROTOCOL_RETURNED_FOR_REVISION', 'Протокол возвращён на доработку'
        )
        PROTOCOL_APPROVED = 'PROTOCOL_APPROVED', 'Протокол согласован'
        PROTOCOL_TASK_ASSIGNED = 'PROTOCOL_TASK_ASSIGNED', 'Назначена задача по протоколу'
        ACT_REJECTION_ASSIGNED = 'ACT_REJECTION_ASSIGNED', 'Назначена задача ПДО по браку'
        SMK_TASK_ASSIGNED = 'SMK_TASK_ASSIGNED', 'Назначена задача СМК'
        BUG_REPORTED = 'BUG_REPORTED', 'Сообщение об ошибке в системе'

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Получатель',
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name='created_notifications',
        verbose_name='Инициатор',
        blank=True,
        null=True,
    )
    event_type = models.CharField('Тип события', max_length=40, choices=EventType.choices)
    title = models.CharField('Заголовок', max_length=200)
    message = models.TextField('Сообщение')
    # `default` is what let the existing production rows take a value when the
    # column was added, and it keeps a source type from ever being NULL. It is
    # not a licence to omit the field: every service resolves it from the
    # source object it was given, and a protocol- or task-shaped notification
    # left on the default is refused by the constraint below.
    source_type = models.CharField(
        'Тип источника',
        max_length=16,
        choices=SourceType.choices,
        default=SourceType.ACT,
    )
    related_act = models.ForeignKey(
        'acts.Act',
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Связанный акт',
        blank=True,
        null=True,
    )
    # Lazy string references on purpose: `tasks.models` already imports
    # `protocols.models`, and neither app becomes part of this one's import
    # graph because of a notification.
    related_protocol = models.ForeignKey(
        'protocols.Protocol',
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Связанный протокол',
        blank=True,
        null=True,
    )
    related_task = models.ForeignKey(
        'tasks.Task',
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Связанная задача',
        blank=True,
        null=True,
    )
    # A report of a broken *page*, not of a business object: it belongs to no
    # act, protocol or task, which is exactly why it needed a source type of
    # its own rather than being squeezed into one of theirs.
    related_bug_report = models.ForeignKey(
        'bugs.BugReport',
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Связанное сообщение об ошибке',
        blank=True,
        null=True,
    )
    deduplication_key = models.CharField('Ключ дедупликации', max_length=180)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    is_read = models.BooleanField('Прочитано', default=False)
    read_at = models.DateTimeField('Прочитано в', blank=True, null=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        indexes = [
            models.Index(fields=['recipient', 'is_read', '-created_at'], name='notif_rec_read_created_idx'),
            models.Index(fields=['related_act', '-created_at'], name='notif_act_created_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['recipient', 'deduplication_key'],
                name='unique_notification_recipient_event',
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(is_read=False, read_at__isnull=True)
                    | models.Q(is_read=True, read_at__isnull=False)
                ),
                name='notification_read_state_consistent',
            ),
            # One constraint listing all three valid shapes, rather than one
            # per type: written this way an unknown `source_type` matches no
            # branch and is rejected too, so the table can never hold a mixed
            # or sourceless notification.
            models.CheckConstraint(
                condition=(
                    models.Q(
                        source_type='ACT',
                        related_act__isnull=False,
                        related_protocol__isnull=True,
                        related_task__isnull=True,
                        related_bug_report__isnull=True,
                    )
                    | models.Q(
                        source_type='PROTOCOL',
                        related_act__isnull=True,
                        related_protocol__isnull=False,
                        related_task__isnull=True,
                        related_bug_report__isnull=True,
                    )
                    | models.Q(
                        source_type='TASK',
                        related_act__isnull=True,
                        related_protocol__isnull=True,
                        related_task__isnull=False,
                        related_bug_report__isnull=True,
                    )
                    | models.Q(
                        source_type='BUG',
                        related_act__isnull=True,
                        related_protocol__isnull=True,
                        related_task__isnull=True,
                        related_bug_report__isnull=False,
                    )
                ),
                name='notification_source_relations_match_source_type',
            ),
        ]
        verbose_name = 'Уведомление'
        verbose_name_plural = 'Уведомления'

    def __str__(self):
        return f'{self.recipient}: {self.title}'

    def clean(self):
        """Readable source validation, restating the check constraint.

        A service or a form gets a field error instead of an `IntegrityError`.
        """
        super().clean()
        required, forbidden = {
            self.SourceType.ACT: (
                'related_act',
                ('related_protocol', 'related_task', 'related_bug_report'),
            ),
            self.SourceType.PROTOCOL: (
                'related_protocol',
                ('related_act', 'related_task', 'related_bug_report'),
            ),
            self.SourceType.TASK: (
                'related_task',
                ('related_act', 'related_protocol', 'related_bug_report'),
            ),
            self.SourceType.BUG: (
                'related_bug_report',
                ('related_act', 'related_protocol', 'related_task'),
            ),
        }.get(self.source_type, (None, ()))
        if required is None:
            raise ValidationError({'source_type': 'Неизвестный тип источника уведомления.'})
        errors = {}
        source_name = self.get_source_type_display()
        if getattr(self, f'{required}_id') is None:
            errors[required] = f'Обязательно для источника «{source_name}».'
        for name in forbidden:
            if getattr(self, f'{name}_id') is not None:
                errors[name] = f'Недопустимо для источника «{source_name}».'
        if errors:
            raise ValidationError(errors)

    def mark_read(self, when=None):
        if self.is_read:
            return False
        self.is_read = True
        self.read_at = when or timezone.now()
        self.save(update_fields=['is_read', 'read_at'])
        return True

    @property
    def related_url(self):
        from .services import get_notification_url

        return get_notification_url(self)

    @property
    def related_label(self):
        """The caption of the «открыть» link, by source type."""
        from .services import get_notification_open_label

        return get_notification_open_label(self)


class NotificationDelivery(models.Model):
    class Channel(models.TextChoices):
        EMAIL = 'email', 'Email'

    class Status(models.TextChoices):
        PENDING = 'pending', 'Ожидает отправки'
        PROCESSING = 'processing', 'Отправляется'
        SENT = 'sent', 'Отправлено'
        FAILED = 'failed', 'Ошибка'
        SKIPPED = 'skipped', 'Пропущено'

    notification = models.ForeignKey(
        Notification,
        on_delete=models.CASCADE,
        related_name='deliveries',
        verbose_name='Уведомление',
    )
    channel = models.CharField('Канал', max_length=20, choices=Channel.choices)
    status = models.CharField('Статус', max_length=20, choices=Status.choices, default=Status.PENDING)
    attempts = models.PositiveIntegerField('Попытки', default=0)
    available_at = models.DateTimeField('Доступно для отправки', default=timezone.now)
    started_at = models.DateTimeField('Начало обработки', blank=True, null=True)
    last_attempt_at = models.DateTimeField('Последняя попытка', blank=True, null=True)
    sent_at = models.DateTimeField('Отправлено в', blank=True, null=True)
    last_error = models.CharField('Последняя ошибка', max_length=500, blank=True)
    created_at = models.DateTimeField('Создано', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлено', auto_now=True)

    class Meta:
        ordering = ['created_at', 'pk']
        indexes = [
            models.Index(fields=['channel', 'status', 'available_at'], name='delivery_queue_idx'),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['notification', 'channel'],
                name='unique_notification_delivery_channel',
            ),
        ]
        verbose_name = 'Доставка уведомления'
        verbose_name_plural = 'Доставки уведомлений'

    def __str__(self):
        return f'{self.notification_id}: {self.get_channel_display()} — {self.get_status_display()}'
