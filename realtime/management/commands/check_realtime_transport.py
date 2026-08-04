"""Diagnose the Redis transport end to end, without touching business data."""

import time
import uuid

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError

from realtime.channels import diagnostic_channel, normalize_channel_prefix
from realtime.transport import (
    describe_failure,
    get_connect_timeout,
    get_socket_timeout,
    redis_exception_types,
    safe_redis_location,
    sanitize,
    sync_client,
)


class Command(BaseCommand):
    help = (
        'Проверяет транспорт real-time: настройки, Redis PING и полный '
        'round trip публикации через одноразовый диагностический канал. '
        'Бизнес-объекты не создаёт и пользовательские каналы не использует.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--timeout',
            type=float,
            default=5.0,
            help='Сколько секунд ждать собственное сообщение (по умолчанию 5).',
        )

    def handle(self, *args, **options):
        wait_timeout = options['timeout']
        if wait_timeout <= 0:
            raise CommandError('--timeout должен быть положительным.')

        self._report_settings()

        client = None
        pubsub = None
        # A one-off channel outside the user:/act: namespaces, so a diagnostic
        # run can never reach a real recipient.
        token = uuid.uuid4().hex
        channel = diagnostic_channel(token)
        try:
            client = self._build_client()
            self._ping(client)

            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(channel)
            except redis_exception_types() as exc:
                raise CommandError(
                    f'Не удалось подписаться на диагностический канал: {describe_failure(exc)}'
                ) from exc
            self.stdout.write(f'Подписка на диагностический канал: {channel}')

            round_trip = self._round_trip(client, pubsub, channel, token, wait_timeout)
        finally:
            self._close(pubsub, channel, client)

        self.stdout.write(f'Round trip: {round_trip * 1000:.1f} мс')
        self.stdout.write(
            self.style.SUCCESS(
                'Транспорт real-time доступен: PING, публикация и подписка работают.'
            )
        )

    # -- steps -----------------------------------------------------------

    def _report_settings(self):
        try:
            prefix = normalize_channel_prefix()
            location = safe_redis_location()
        except (ImproperlyConfigured, ValueError) as exc:
            raise CommandError(f'Некорректные настройки real-time: {exc}') from exc

        self.stdout.write(f'REALTIME_ENABLED         — {getattr(settings, "REALTIME_ENABLED", False)}')
        self.stdout.write(f'Publisher backend        — {getattr(settings, "REALTIME_PUBLISHER_BACKEND", "")}')
        # The location is printed without credentials on purpose.
        self.stdout.write(f'Redis                    — {location}')
        self.stdout.write(f'Префикс каналов          — {prefix}')
        self.stdout.write(f'Connect / socket timeout — {get_connect_timeout()} / {get_socket_timeout()} с')
        self.stdout.write(f'Heartbeat                — {getattr(settings, "REALTIME_HEARTBEAT_SECONDS", "")} с')
        self.stdout.write(f'Максимальный размер      — {getattr(settings, "REALTIME_MAX_EVENT_BYTES", "")} байт')
        if not getattr(settings, 'REALTIME_ENABLED', False):
            self.stdout.write(
                self.style.WARNING(
                    'REALTIME_ENABLED=false: транспорт проверяется, но события не публикуются.'
                )
            )

    def _build_client(self):
        try:
            return sync_client()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        except redis_exception_types() as exc:
            raise CommandError(f'Не удалось создать Redis-клиент: {describe_failure(exc)}') from exc

    def _ping(self, client):
        try:
            client.ping()
        except redis_exception_types() as exc:
            raise CommandError(f'Redis не отвечает на PING: {describe_failure(exc)}') from exc
        self.stdout.write('PING — ok')

    def _round_trip(self, client, pubsub, channel, token, wait_timeout):
        started = time.monotonic()
        try:
            client.publish(channel, token)
        except redis_exception_types() as exc:
            raise CommandError(
                f'Не удалось опубликовать диагностическое сообщение: {describe_failure(exc)}'
            ) from exc

        deadline = started + wait_timeout
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                message = pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=min(remaining, 1.0)
                )
            except redis_exception_types() as exc:
                raise CommandError(
                    f'Ошибка чтения из диагностического канала: {describe_failure(exc)}'
                ) from exc
            if message is None or message.get('type') != 'message':
                continue
            received = message.get('data')
            if isinstance(received, (bytes, bytearray)):
                received = bytes(received).decode('utf-8', errors='replace')
            if received != token:
                raise CommandError(
                    'Получено сообщение, не совпадающее с отправленным token: '
                    'канал используется чем-то ещё.'
                )
            return time.monotonic() - started

        raise CommandError(
            f'Диагностическое сообщение не получено за {wait_timeout} с. '
            f'Проверьте доступность {safe_redis_location()}.'
        )

    def _close(self, pubsub, channel, client):
        if pubsub is not None:
            for step, action in (
                ('unsubscribe', lambda: pubsub.unsubscribe(channel)),
                ('close', pubsub.close),
            ):
                try:
                    action()
                except Exception as exc:  # noqa: BLE001 - teardown must never raise
                    self.stdout.write(
                        self.style.WARNING(
                            f'Не удалось выполнить {step} диагностического канала: '
                            f'{sanitize(type(exc).__name__)}'
                        )
                    )
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                self.stdout.write(
                    self.style.WARNING(
                        f'Не удалось закрыть Redis-клиент: {sanitize(type(exc).__name__)}'
                    )
                )
