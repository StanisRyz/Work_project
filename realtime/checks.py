"""Django system checks for the real-time configuration.

A misconfigured real-time stack fails quietly — the stream simply never
delivers anything — so the dangerous combinations are surfaced by
`manage.py check` instead.

Severity follows DEBUG: a half-finished development setup is a warning, while
the same configuration with `DEBUG=False` is an error. No message ever contains
the Redis password or a URL with credentials: only the sanitized location from
`realtime.transport.safe_redis_location`.
"""

from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Warning, register


REDIS_PUBLISHER = 'realtime.backends.RedisRealtimePublisher'
NOOP_PUBLISHER = 'realtime.backends.NoopRealtimePublisher'
ALLOWED_SCHEMES = ('redis', 'rediss')


def _problem(debug_is_warning=True):
    """Warning while developing, Error once DEBUG is off."""
    if getattr(settings, 'DEBUG', False) and debug_is_warning:
        return Warning
    return Error


def _uses_redis_publisher():
    return getattr(settings, 'REALTIME_PUBLISHER_BACKEND', NOOP_PUBLISHER) == REDIS_PUBLISHER


@register()
def check_realtime_configuration(app_configs, **kwargs):
    errors = []
    enabled = bool(getattr(settings, 'REALTIME_ENABLED', False))

    if enabled and not _uses_redis_publisher():
        errors.append(
            _problem()(
                'REALTIME_ENABLED=true, но выбран publisher без транспорта.',
                hint=(
                    'События формируются, но никуда не отправляются. Укажите '
                    f'REALTIME_PUBLISHER_BACKEND={REDIS_PUBLISHER} или выключите '
                    'REALTIME_ENABLED.'
                ),
                id='realtime.E001',
            )
        )

    if enabled or _uses_redis_publisher():
        errors.extend(_check_redis_url())
        errors.extend(_check_channel_prefix())
        errors.extend(_check_asgi_application())

    errors.extend(_check_intervals())
    return errors


def _check_redis_url():
    from .transport import safe_redis_location

    raw = str(getattr(settings, 'REALTIME_REDIS_URL', '') or '').strip()
    if not raw:
        return [
            _problem(debug_is_warning=False)(
                'REALTIME_REDIS_URL не задан, а Redis-транспорт выбран.',
                hint='Укажите REALTIME_REDIS_URL вида redis://host:port/db.',
                id='realtime.E002',
            )
        ]
    try:
        parts = urlsplit(raw)
    except ValueError:
        parts = None
    if parts is None or not parts.scheme or not parts.hostname:
        return [
            _problem(debug_is_warning=False)(
                'REALTIME_REDIS_URL не является корректным URL.',
                hint='Ожидается redis://host:port/db или rediss://host:port/db.',
                id='realtime.E003',
            )
        ]
    if parts.scheme not in ALLOWED_SCHEMES:
        return [
            _problem(debug_is_warning=False)(
                f'Недопустимая схема REALTIME_REDIS_URL: {parts.scheme}://',
                hint=(
                    'Поддерживаются только redis:// и rediss://. Текущий адрес: '
                    f'{safe_redis_location(raw)}.'
                ),
                id='realtime.E004',
            )
        ]
    return []


def _check_channel_prefix():
    from .channels import RealtimeChannelError, normalize_channel_prefix

    try:
        normalize_channel_prefix()
    except RealtimeChannelError as exc:
        return [
            _problem(debug_is_warning=False)(
                f'Некорректный REALTIME_CHANNEL_PREFIX: {exc}',
                hint='Допустимы латинские буквы, цифры и . _ : - (до 64 символов).',
                id='realtime.E005',
            )
        ]
    return []


def _check_asgi_application():
    if not str(getattr(settings, 'ASGI_APPLICATION', '') or '').strip():
        return [
            _problem()(
                'ASGI_APPLICATION не задан, а SSE-endpoint требует ASGI.',
                hint=(
                    'Укажите ASGI_APPLICATION и запускайте проект ASGI-сервером '
                    '(например, uvicorn ecosystem.asgi:application). WSGI не умеет '
                    'держать длительный поток.'
                ),
                id='realtime.E006',
            )
        ]
    return []


def _check_intervals():
    errors = []
    heartbeat = float(getattr(settings, 'REALTIME_HEARTBEAT_SECONDS', 25.0))
    max_connection = float(getattr(settings, 'REALTIME_MAX_CONNECTION_SECONDS', 900.0))
    if heartbeat >= max_connection:
        errors.append(
            _problem(debug_is_warning=False)(
                'REALTIME_HEARTBEAT_SECONDS не меньше REALTIME_MAX_CONNECTION_SECONDS.',
                hint=(
                    f'Heartbeat ({heartbeat} с) должен быть заметно меньше времени жизни '
                    f'соединения ({max_connection} с), иначе поток закроется, ни разу не '
                    'подтвердив активность.'
                ),
                id='realtime.E007',
            )
        )

    visible = float(getattr(settings, 'REALTIME_SYNC_POLL_SECONDS', 30.0))
    hidden = float(getattr(settings, 'REALTIME_SYNC_HIDDEN_POLL_SECONDS', 90.0))
    if hidden < visible:
        errors.append(
            _problem()(
                'REALTIME_SYNC_HIDDEN_POLL_SECONDS меньше REALTIME_SYNC_POLL_SECONDS.',
                hint='Скрытая вкладка должна опрашивать сервер реже активной, а не чаще.',
                id='realtime.W001',
            )
        )

    lease = float(getattr(settings, 'REALTIME_LEADER_LEASE_SECONDS', 12.0))
    lease_heartbeat = float(getattr(settings, 'REALTIME_LEADER_HEARTBEAT_SECONDS', 4.0))
    if lease_heartbeat >= lease:
        errors.append(
            _problem(debug_is_warning=False)(
                'REALTIME_LEADER_HEARTBEAT_SECONDS не меньше REALTIME_LEADER_LEASE_SECONDS.',
                hint=(
                    'Лидер обязан продлевать аренду чаще, чем она истекает, иначе вкладки '
                    'будут постоянно переизбирать лидера.'
                ),
                id='realtime.E008',
            )
        )
    return errors
