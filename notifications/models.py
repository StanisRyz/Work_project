from django.conf import settings
from django.db import models
from django.utils import timezone


class Notification(models.Model):
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
    related_act = models.ForeignKey(
        'acts.Act',
        on_delete=models.CASCADE,
        related_name='notifications',
        verbose_name='Связанный акт',
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
        ]
        verbose_name = 'Уведомление'
        verbose_name_plural = 'Уведомления'

    def __str__(self):
        return f'{self.recipient}: {self.title}'

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
