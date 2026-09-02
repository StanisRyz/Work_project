"""Manually mail initial onboarding credentials to one user or to everybody.

Deliberately *not* a business event: it creates no `Notification`, no
`NotificationDelivery` and no synthetic act, protocol or task source. It is an
administrator's one-off action, sent straight through the configured Django
email backend with the same SMTP settings the notification queue uses.

The only credential it can state is the one the account was created with — the
username as its own password — and it says so only after
`check_password(username)` confirms that this is still true. Django stores no
raw password, so nothing here reads or reconstructs one; a user who has already
changed theirs is skipped rather than sent a wrong password.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string

from ecosystem.logging_utils import log_event
from notifications.email_delivery import sanitize_error


logger = logging.getLogger('notifications.email')

ALL_TARGET = 'ALL'
SUBJECT = '[Экосистема качества] Добро пожаловать в систему'


class Command(BaseCommand):
    help = (
        'Отправляет письмо с первоначальными учётными данными одному '
        'пользователю по логину или всем активным пользователям (ALL).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            'target',
            help='Точный логин пользователя или ALL для рассылки всем активным пользователям.',
        )

    def handle(self, *args, **options):
        if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
            raise CommandError(
                'EMAIL_NOTIFICATIONS_ENABLED=false: отправка писем отключена. '
                'Включите почтовую конфигурацию перед рассылкой.'
            )
        target = options['target'].strip()
        if target.upper() == ALL_TARGET:
            return self._handle_all()
        return self._handle_one(target)

    # -- single user ------------------------------------------------------

    def _handle_one(self, username):
        try:
            user = get_user_model().objects.get(username=username, is_active=True)
        except get_user_model().DoesNotExist as exc:
            raise CommandError(f'Активный пользователь с логином «{username}» не найден.') from exc
        if not (user.email or '').strip():
            raise CommandError(f'У пользователя «{username}» не указан email-адрес.')
        if not user.check_password(user.username):
            raise CommandError(
                f'Пароль пользователя «{username}» больше не совпадает с первоначальным '
                'паролем, равным логину. Письмо не отправлено.'
            )
        try:
            self._send(user)
        except Exception as exc:
            # The exception type and a scrubbed message only: a raw SMTP
            # response can quote the relay account or an auth failure.
            raise CommandError(f'Не удалось отправить письмо: {sanitize_error(exc)}') from exc
        self.stdout.write(self.style.SUCCESS(f'Письмо отправлено пользователю «{username}».'))

    # -- everybody --------------------------------------------------------

    def _handle_all(self):
        users = get_user_model().objects.filter(is_active=True).order_by('pk')
        summary = {'sent': 0, 'skipped_no_email': 0, 'skipped_password_changed': 0, 'failed': 0}
        for user in users:
            if not (user.email or '').strip():
                summary['skipped_no_email'] += 1
                continue
            if not user.check_password(user.username):
                summary['skipped_password_changed'] += 1
                continue
            try:
                # One personalized message per person — never one message with
                # everybody in BCC, which would leak the whole address book.
                self._send(user)
            except Exception as exc:
                summary['failed'] += 1
                log_event(
                    logger,
                    'ERROR',
                    'email.welcome_failed',
                    user_id=user.pk,
                    error_type=type(exc).__name__,
                    outcome='failed',
                )
                continue
            summary['sent'] += 1
        self.stdout.write(
            'Рассылка завершена: '
            f"отправлено — {summary['sent']}, "
            f"без email — {summary['skipped_no_email']}, "
            f"пароль изменён — {summary['skipped_password_changed']}, "
            f"ошибок — {summary['failed']}."
        )
        if summary['failed']:
            raise CommandError(
                f"Не доставлено писем: {summary['failed']}. Подробности — в журнале "
                "notifications.email."
            )

    # -- delivery ---------------------------------------------------------

    def _send(self, user):
        context = {
            'username': user.get_username(),
            'app_url': settings.APP_BASE_URL,
        }
        message = EmailMultiAlternatives(
            subject=SUBJECT,
            body=render_to_string('notifications/email/welcome.txt', context),
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[user.email],
        )
        message.attach_alternative(
            render_to_string('notifications/email/welcome.html', context), 'text/html'
        )
        sent = message.send(fail_silently=False)
        if sent != 1:
            raise RuntimeError('Почтовый backend не подтвердил отправку сообщения.')
        # Identifiers only — never the address, the body or the credential.
        log_event(logger, 'INFO', 'email.welcome_sent', user_id=user.pk, outcome='sent')
