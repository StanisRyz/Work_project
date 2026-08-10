"""The personal SSE endpoint.

One rule governs this module: the stream a user gets is derived from
`request.user` and nothing else. Query string, path and request body never
influence the subscription, so a client cannot ask to listen to somebody else.
"""

import logging
import time

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import connections
from django.http import (
    HttpResponse,
    HttpResponseNotAllowed,
    JsonResponse,
    StreamingHttpResponse,
)
from django.views.decorators.http import require_GET

from ecosystem.logging_utils import log_event

from .auth import realtime_login_required
from .publisher import realtime_enabled
from .sse import event_stream, redis_is_reachable
from .sync import build_sync_state


logger = logging.getLogger('realtime')

SSE_CONTENT_TYPE = 'text/event-stream'

# A sync slower than this is worth a log line: it usually means an unindexed
# aggregate on a growing table.
SLOW_SYNC_SECONDS = 1.0


def _close_sse_database_connections():
    # Async auth/session ORM uses this request's thread-sensitive executor.
    for database_connection in connections.all(initialized_only=True):
        if not database_connection.in_atomic_block:
            database_connection.close()


async def realtime_events(request):
    """`GET /realtime/events/` — the authenticated user's own event stream."""
    if request.method != 'GET':
        return HttpResponseNotAllowed(['GET'])

    # The session is the only source of identity here; there is no token or
    # query parameter that could name a different user.
    user = await request.auser()
    if not user.is_authenticated:
        return HttpResponse(status=401)
    user_id = int(user.pk)

    if not realtime_enabled():
        # Nothing to stream and no reason to touch Redis at all.
        return HttpResponse(status=204)

    # Do this in the same thread-sensitive context that resolved the session.
    await sync_to_async(_close_sse_database_connections, thread_sensitive=True)()

    reachable, reason = await redis_is_reachable()
    if not reachable:
        # `reason` comes from `describe_failure`, which keeps the host visible
        # for diagnosis but never the URL, username or password.
        log_event(
            logger,
            'WARNING',
            'realtime.stream_refused',
            user_id=user_id,
            detail=reason,
            outcome='redis_unavailable',
        )
        return HttpResponse(status=503)

    response = StreamingHttpResponse(
        event_stream(user_id),
        content_type=SSE_CONTENT_TYPE,
    )
    return _apply_stream_headers(response)


@realtime_login_required
@require_GET
def realtime_sync(request):
    """`GET /realtime/sync/` — opaque revision tokens for the current user.

    The recovery path when events were missed. The user comes from the session
    only: no user id or target is accepted, nothing is modified, and the
    response carries no Redis configuration and no foreign object data.
    """
    started = time.monotonic()
    state = build_sync_state(request.user)
    duration = time.monotonic() - started

    if duration >= SLOW_SYNC_SECONDS:
        log_event(
            logger,
            'WARNING',
            'realtime.slow_sync',
            user_id=request.user.pk,
            duration_ms=duration * 1000,
            outcome='slow',
        )
    else:
        # DEBUG: a fast sync that changed nothing runs on every connect,
        # reconnect and safety timer — at INFO it would drown the file.
        log_event(
            logger,
            'DEBUG',
            'realtime.sync_completed',
            user_id=request.user.pk,
            duration_ms=duration * 1000,
            outcome='ok',
        )

    response = JsonResponse(state)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response['Vary'] = 'Cookie'
    return response


def _apply_stream_headers(response):
    """Headers a streaming SSE response needs to survive proxies and caches.

    Hop-by-hop headers (`Connection`, `Upgrade`) are deliberately not set: the
    ASGI server owns them.
    """
    response['Cache-Control'] = 'no-cache, no-store, no-transform'
    response['X-Accel-Buffering'] = 'no'
    response['Vary'] = 'Cookie'
    return response
