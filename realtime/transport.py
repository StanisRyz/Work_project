"""Redis connection handling for the real-time transport.

Everything here is lazy: the `redis` package is imported only when a Redis
publisher or the SSE stream is actually used, so a project running with
`REALTIME_ENABLED=false` never builds a client, never opens a socket and never
needs a Redis server — `manage.py check` included.

Credentials never leave this module: callers get a sanitized location string
(`redis://host:port/db`) and sanitized error text.
"""

import logging
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver


logger = logging.getLogger('realtime')

REDACTED = '***'

_sync_pool = None
_sync_pool_key = None


class RealtimeTransportError(Exception):
    """The transport could not be reached or used."""


# --------------------------------------------------------------------------
# Lazy imports
# --------------------------------------------------------------------------

def import_redis():
    """Return the synchronous redis module, or explain why it is unavailable."""
    try:
        import redis  # noqa: PLC0415 - deliberately lazy
    except ImportError as exc:  # pragma: no cover - requirements pin the package
        raise ImproperlyConfigured(
            'Для Redis-транспорта real-time нужен пакет redis. '
            'Установите зависимости: python -m pip install -r requirements.txt.'
        ) from exc
    return redis


def import_async_redis():
    """Return the asyncio redis module, or explain why it is unavailable."""
    import_redis()
    import redis.asyncio as async_redis  # noqa: PLC0415 - deliberately lazy

    return async_redis


def redis_exception_types():
    """Return the exception classes the transport treats as Redis failures."""
    exceptions = import_redis().exceptions
    return (exceptions.RedisError, OSError)


# --------------------------------------------------------------------------
# Settings and redaction
# --------------------------------------------------------------------------

def get_redis_url():
    url = str(getattr(settings, 'REALTIME_REDIS_URL', '') or '').strip()
    if not url:
        raise ImproperlyConfigured(
            'REALTIME_REDIS_URL не задан, а выбран Redis-транспорт real-time.'
        )
    return url


def get_connect_timeout():
    return float(getattr(settings, 'REALTIME_REDIS_CONNECT_TIMEOUT_SECONDS', 5.0))


def get_socket_timeout():
    return float(getattr(settings, 'REALTIME_REDIS_SOCKET_TIMEOUT_SECONDS', 5.0))


def get_max_event_bytes():
    return int(getattr(settings, 'REALTIME_MAX_EVENT_BYTES', 16384))


def safe_redis_location(url=None):
    """Return `scheme://host:port/db` with any username and password removed."""
    raw = url if url is not None else getattr(settings, 'REALTIME_REDIS_URL', '')
    try:
        parts = urlsplit(str(raw or ''))
    except ValueError:
        return REDACTED
    if not parts.scheme:
        return REDACTED
    host = parts.hostname or 'localhost'
    location = f'{parts.scheme}://{host}'
    if parts.port:
        location = f'{location}:{parts.port}'
    if parts.path and parts.path != '/':
        location = f'{location}{parts.path}'
    return location


def _secret_values(url=None):
    """Every substring that must never reach a log line."""
    raw = str(url if url is not None else getattr(settings, 'REALTIME_REDIS_URL', '') or '')
    values = set()
    if raw:
        values.add(raw)
    try:
        parts = urlsplit(raw)
    except ValueError:
        return values
    for candidate in (parts.password, parts.username):
        if candidate:
            values.add(candidate)
    if parts.netloc and '@' in parts.netloc:
        values.add(parts.netloc.rsplit('@', 1)[0])
    return values


def sanitize(text, url=None):
    """Strip the Redis URL and its credentials out of arbitrary text."""
    cleaned = str(text)
    for secret in sorted(_secret_values(url), key=len, reverse=True):
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def describe_failure(exc, url=None):
    """A log-safe description of a transport failure."""
    return (
        f'{type(exc).__name__}: {sanitize(exc, url)} '
        f'(адрес: {safe_redis_location(url)})'
    )


# --------------------------------------------------------------------------
# Clients
# --------------------------------------------------------------------------

def sync_connection_pool():
    """Return a process-wide connection pool, built at most once per URL."""
    global _sync_pool, _sync_pool_key
    redis = import_redis()
    url = get_redis_url()
    key = (url, get_connect_timeout(), get_socket_timeout())
    if _sync_pool is None or _sync_pool_key != key:
        close_sync_pool()
        _sync_pool = redis.ConnectionPool.from_url(
            url,
            socket_connect_timeout=get_connect_timeout(),
            socket_timeout=get_socket_timeout(),
            health_check_interval=30,
        )
        _sync_pool_key = key
    return _sync_pool


def sync_client():
    """Return a synchronous client sharing the cached pool.

    Used from the `on_commit` callback, where a long retry would sit inside the
    user's request: the socket timeouts above keep a Redis outage brief.
    """
    redis = import_redis()
    return redis.Redis(connection_pool=sync_connection_pool())


def close_sync_pool():
    global _sync_pool, _sync_pool_key
    if _sync_pool is not None:
        try:
            _sync_pool.disconnect()
        except Exception:  # noqa: BLE001 - teardown must not raise
            pass
    _sync_pool = None
    _sync_pool_key = None


def async_client():
    """Return a fresh asyncio client for one SSE connection.

    Not pooled on purpose: a streaming subscriber holds its connection for the
    whole request, and each connection is closed explicitly on disconnect.
    """
    async_redis = import_async_redis()
    return async_redis.Redis.from_url(
        get_redis_url(),
        socket_connect_timeout=get_connect_timeout(),
        socket_timeout=get_socket_timeout(),
        health_check_interval=30,
    )


@receiver(setting_changed)
def _reset_pool_on_setting_change(sender, setting, **kwargs):
    if setting in {
        'REALTIME_REDIS_URL',
        'REALTIME_REDIS_CONNECT_TIMEOUT_SECONDS',
        'REALTIME_REDIS_SOCKET_TIMEOUT_SECONDS',
    }:
        close_sync_pool()
