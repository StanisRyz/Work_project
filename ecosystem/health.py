"""Liveness and readiness endpoints for a process manager or load balancer.

Two deliberately different questions:

* **liveness** — «is this process alive at all?» It touches nothing: no
  database, no Redis, no SMTP, no filesystem. A dependency being down must
  never make a healthy process look dead and get restarted in a loop.
* **readiness** — «can this process serve real traffic right now?» It checks
  the dependencies it genuinely needs, read-only.

Neither endpoint reveals infrastructure. A failing readiness probe answers
`{"status": "unavailable"}` and nothing else: no SQL, no exception text, no
path, host, username, Redis URL or credential. The detail goes to the
`deployment` logger, where the operator can already see it.
"""

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods


logger = logging.getLogger('deployment')

REDIS_PUBLISHER = 'realtime.backends.RedisRealtimePublisher'


def _no_store(response):
    """A health answer describes this instant and must never be cached."""
    response['Cache-Control'] = 'no-store'
    return response


@require_http_methods(['GET', 'HEAD'])
def health_live(request):
    """`GET /health/live/` — the process is running. Nothing else is claimed."""
    return _no_store(JsonResponse({'status': 'ok'}))


@require_http_methods(['GET', 'HEAD'])
def health_ready(request):
    """`GET /health/ready/` — every required dependency answers.

    Read-only: it runs `SELECT 1`, inspects migration state, optionally pings
    Redis, and stats two directories. It writes nothing anywhere.
    """
    failures = []

    for name, probe in (
        ('database', _check_database),
        ('migrations', _check_migrations),
        ('redis', _check_redis),
        ('media_root', _check_media_root),
        ('static_root', _check_static_root),
    ):
        try:
            detail = probe()
        except Exception as exc:  # noqa: BLE001 - a probe must never 500
            detail = f'{type(exc).__name__}'
        if detail:
            failures.append((name, detail))

    if failures:
        # The operator gets the detail in the log; the response body does not
        # describe the infrastructure to whoever called the endpoint.
        logger.warning(
            'deployment.readiness_failed checks=%(checks)s',
            {'checks': ', '.join(f'{name}:{detail}' for name, detail in failures)},
        )
        return _no_store(JsonResponse({'status': 'unavailable'}, status=503))

    return _no_store(JsonResponse({'status': 'ready'}))


# -- probes -----------------------------------------------------------------
#
# Every probe returns an empty string when healthy, or a short, safe reason
# when not. A reason names the *kind* of problem, never a value.

def _check_database():
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return ''


def _check_migrations():
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    targets = executor.loader.graph.leaf_nodes()
    plan = executor.migration_plan(targets)
    if plan:
        return f'pending={len(plan)}'
    return ''


def _check_redis():
    """Only meaningful when real-time actually depends on Redis."""
    if not getattr(settings, 'REALTIME_ENABLED', False):
        return ''
    if getattr(settings, 'REALTIME_PUBLISHER_BACKEND', '') != REDIS_PUBLISHER:
        return ''
    from realtime.transport import redis_exception_types, sync_client

    client = sync_client()
    try:
        client.ping()
    except redis_exception_types():
        # Never the URL and never the credentials — just the fact.
        return 'ping_failed'
    return ''


def _check_media_root():
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        return 'not_configured'
    import os
    from pathlib import Path

    path = Path(media_root)
    if not path.is_dir():
        return 'missing'
    if not os.access(path, os.W_OK):
        return 'not_writable'
    return ''


def _check_static_root():
    static_root = getattr(settings, 'STATIC_ROOT', None)
    if not static_root:
        return 'not_configured'
    from pathlib import Path

    # Existence is only required once `collectstatic` has run, which is a
    # deployment step; a missing directory in development is not a failure.
    if getattr(settings, 'IS_PRODUCTION', False) and not Path(static_root).is_dir():
        return 'missing'
    return ''
