import re
import smtplib
from datetime import timedelta

from django.conf import settings
from django.core.mail import BadHeaderError, EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone

from .models import NotificationDelivery
from .services import get_notification_url, get_required_action


PERMANENT_SMTP_EXCEPTIONS = (
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPNotSupportedError,
    BadHeaderError,
)


def process_pending_deliveries(batch_size=None):
    batch_size = batch_size or settings.EMAIL_NOTIFICATION_BATCH_SIZE
    _recover_stale_deliveries()
    delivery_ids = list(
        NotificationDelivery.objects.filter(
            channel=NotificationDelivery.Channel.EMAIL,
            status=NotificationDelivery.Status.PENDING,
            available_at__lte=timezone.now(),
            attempts__lt=settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS,
        )
        .order_by('available_at', 'pk')
        .values_list('pk', flat=True)[:batch_size]
    )
    summary = {status: 0 for status in ('sent', 'pending', 'failed', 'skipped')}
    for delivery_id in delivery_ids:
        status = process_delivery(delivery_id)
        if status in summary:
            summary[status] += 1
    return summary


def process_delivery(delivery_id):
    with transaction.atomic():
        delivery = (
            NotificationDelivery.objects.select_for_update()
            .select_related('notification__recipient', 'notification__actor', 'notification__related_act')
            .get(pk=delivery_id)
        )
        if delivery.status != NotificationDelivery.Status.PENDING:
            return delivery.status
        if not settings.EMAIL_NOTIFICATIONS_ENABLED:
            delivery.status = NotificationDelivery.Status.SKIPPED
            delivery.last_error = 'Email-уведомления отключены настройкой EMAIL_NOTIFICATIONS_ENABLED.'
            delivery.save(update_fields=['status', 'last_error', 'updated_at'])
            return delivery.status
        if not delivery.notification.recipient.email.strip():
            delivery.status = NotificationDelivery.Status.SKIPPED
            delivery.last_error = 'У получателя не указан email-адрес.'
            delivery.save(update_fields=['status', 'last_error', 'updated_at'])
            return delivery.status
        if delivery.attempts >= settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS:
            delivery.status = NotificationDelivery.Status.FAILED
            delivery.last_error = 'Достигнут предел попыток отправки.'
            delivery.save(update_fields=['status', 'last_error', 'updated_at'])
            return delivery.status

        now = timezone.now()
        delivery.status = NotificationDelivery.Status.PROCESSING
        delivery.attempts += 1
        delivery.started_at = now
        delivery.last_attempt_at = now
        delivery.last_error = ''
        delivery.save(
            update_fields=[
                'status',
                'attempts',
                'started_at',
                'last_attempt_at',
                'last_error',
                'updated_at',
            ]
        )
        notification = delivery.notification

    try:
        sent_count = _send_email(notification)
        if sent_count != 1:
            raise RuntimeError('Почтовый backend не подтвердил отправку сообщения.')
    except Exception as exc:  # The delivery boundary must never affect a business transaction.
        return _record_failure(delivery_id, exc)

    now = timezone.now()
    NotificationDelivery.objects.filter(pk=delivery_id).update(
        status=NotificationDelivery.Status.SENT,
        sent_at=now,
        started_at=None,
        last_error='',
        updated_at=now,
    )
    return NotificationDelivery.Status.SENT


def _send_email(notification):
    act_url = get_notification_url(notification, absolute=True)
    actor_name = 'Система'
    if notification.actor:
        actor_name = notification.actor.get_full_name() or notification.actor.get_username()
    context = {
        'notification_title': notification.title,
        'event_name': notification.get_event_type_display(),
        'act_number': notification.related_act.number,
        'required_action': get_required_action(notification),
        'actor_name': actor_name,
        'event_date': timezone.localtime(notification.created_at),
        'act_url': act_url,
    }
    subject = f'[Экосистема качества] {notification.title}'
    text_body = render_to_string('notifications/email/notification.txt', context)
    html_body = render_to_string('notifications/email/notification.html', context)
    message = EmailMultiAlternatives(
        subject=subject,
        body=text_body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[notification.recipient.email],
    )
    message.attach_alternative(html_body, 'text/html')
    return message.send(fail_silently=False)


def _record_failure(delivery_id, exc):
    now = timezone.now()
    delivery = NotificationDelivery.objects.get(pk=delivery_id)
    retryable = _is_retryable(exc)
    can_retry = retryable and delivery.attempts < settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS
    delivery.status = (
        NotificationDelivery.Status.PENDING if can_retry else NotificationDelivery.Status.FAILED
    )
    if can_retry:
        delivery.available_at = now + timedelta(seconds=settings.EMAIL_NOTIFICATION_RETRY_DELAY_SECONDS)
    delivery.started_at = None
    delivery.last_error = _sanitize_error(exc)
    delivery.save(update_fields=['status', 'available_at', 'started_at', 'last_error', 'updated_at'])
    return delivery.status


def _is_retryable(exc):
    if isinstance(exc, PERMANENT_SMTP_EXCEPTIONS):
        return False
    if isinstance(exc, smtplib.SMTPResponseException):
        return 400 <= exc.smtp_code < 500
    return isinstance(exc, (OSError, TimeoutError, smtplib.SMTPException))


def _sanitize_error(exc):
    message = re.sub(r'[\r\n\t]+', ' ', str(exc)).strip()
    for secret in (settings.EMAIL_HOST_PASSWORD, settings.EMAIL_HOST_USER):
        if secret:
            message = message.replace(secret, '[скрыто]')
    return f'{type(exc).__name__}: {message}'[:500]


def _recover_stale_deliveries():
    now = timezone.now()
    NotificationDelivery.objects.filter(
        channel=NotificationDelivery.Channel.EMAIL,
        status=NotificationDelivery.Status.PENDING,
        attempts__gte=settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS,
    ).update(
        status=NotificationDelivery.Status.FAILED,
        last_error='Достигнут предел попыток отправки.',
        updated_at=now,
    )
    stale_before = now - timedelta(seconds=settings.EMAIL_NOTIFICATION_PROCESSING_TIMEOUT_SECONDS)
    stale = NotificationDelivery.objects.filter(
        channel=NotificationDelivery.Channel.EMAIL,
        status=NotificationDelivery.Status.PROCESSING,
        started_at__lt=stale_before,
    )
    stale.filter(attempts__lt=settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS).update(
        status=NotificationDelivery.Status.PENDING,
        available_at=now,
        started_at=None,
        last_error='Предыдущая обработка была прервана; доставка возвращена в очередь.',
        updated_at=now,
    )
    stale.filter(attempts__gte=settings.EMAIL_NOTIFICATION_MAX_ATTEMPTS).update(
        status=NotificationDelivery.Status.FAILED,
        started_at=None,
        last_error='Обработка была прервана; достигнут предел попыток отправки.',
        updated_at=now,
    )
