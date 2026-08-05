"""Per-request logging context and operational request logging.

One middleware, deliberately doing two things that must not drift apart:

* it binds a **fresh** `request_id` and the authenticated `user_id` into the
  ContextVars every other log line reads, and clears them in `finally`;
* it writes at most one `http.request` line per request, and only for requests
  that are worth a line.

**Volume is a design constraint here.** A pilot deployment that logs every GET
produces a file nobody reads and rotates away the one error that mattered. So
the rule is: mutating methods are logged at INFO, and a read is logged only
when it was slow, failed, or raised. Health probes, static files and the
long-lived SSE stream are excluded entirely — the SSE connection lifecycle is
already logged by the `realtime` logger, with its own connection id.

It exists in both a sync and an async form because the project serves the SSE
stream under ASGI: adapting a sync-only middleware would push every request
through a thread executor. ContextVars are per-context in both modes, so two
concurrent requests can never see each other's ids.

Nothing derived from the request body, query string, cookies, headers or the
user's name is ever recorded.
"""

import logging
import time

from asgiref.sync import iscoroutinefunction
from django.conf import settings
from django.utils.decorators import sync_and_async_middleware

from .logging_utils import (
    log_event,
    new_request_id,
    reset_request_context,
    set_request_context,
)


logger = logging.getLogger('ecosystem.request')

REQUEST_ID_HEADER = 'X-Request-ID'

MUTATING_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})

# Paths whose ordinary traffic is pure noise: a process manager polls the
# health endpoints every few seconds, and static assets are served by the web
# server in production anyway.
EXCLUDED_PATH_PREFIXES = ('/static/', '/media/')
HEALTH_PATHS = ('/health/live/', '/health/ready/')
FAVICON_PATHS = ('/favicon.ico',)

# The SSE stream stays open for minutes by design, so the slow-request rule
# would fire on every single connection and say nothing useful. Its lifecycle
# is logged by `realtime.sse` instead, keyed by connection id.
STREAMING_PATHS = ('/realtime/events/',)


def _matches(path, candidates):
    return any(path == candidate or path.startswith(candidate) for candidate in candidates)


def _is_excluded(path):
    """Paths that never produce an ordinary request log line."""
    if _matches(path, EXCLUDED_PATH_PREFIXES) or _matches(path, FAVICON_PATHS):
        return True
    if _matches(path, HEALTH_PATHS) and not getattr(settings, 'LOG_HEALTH_REQUESTS', False):
        return True
    return False


def _route_name(request):
    """The resolved view/route name — never the path with its parameters.

    A raw path can carry identifiers, and a query string can carry anything at
    all; the route name is the stable, safe way to say which endpoint ran.
    """
    match = getattr(request, 'resolver_match', None)
    if match is None:
        return None
    return match.view_name or match.url_name


def _log_request(request, response, duration_ms, request_id, user_id):
    """Write at most one `http.request` line, following the volume policy."""
    if _is_excluded(request.path):
        return

    status = getattr(response, 'status_code', 0)
    method = request.method
    slow_after_ms = float(getattr(settings, 'LOG_SLOW_REQUEST_MS', 2000))
    # A streaming response's measured duration is the time to *start*
    # streaming, so comparing it with the slow threshold is meaningless.
    is_slow = (
        not _matches(request.path, STREAMING_PATHS) and duration_ms >= slow_after_ms
    )

    if status >= 500:
        level, outcome = 'ERROR', 'server_error'
    elif status >= 400:
        level, outcome = 'WARNING', 'client_error'
    elif is_slow:
        level, outcome = 'WARNING', 'slow'
    elif method in MUTATING_METHODS:
        if not getattr(settings, 'LOG_MUTATING_REQUESTS', True):
            return
        level, outcome = 'INFO', 'ok'
    else:
        # An ordinary, fast, successful read: deliberately not logged.
        return

    log_event(
        logger,
        level,
        'http.request',
        method=method,
        route=_route_name(request),
        status=status,
        duration_ms=duration_ms,
        request_id=request_id,
        user_id=user_id,
        outcome=outcome,
    )


def _log_exception(request, duration_ms, request_id, user_id):
    """The failure line: joinable by request id, with the trace attached."""
    log_event(
        logger,
        'ERROR',
        'http.request_failed',
        method=request.method,
        route=_route_name(request),
        duration_ms=duration_ms,
        request_id=request_id,
        user_id=user_id,
        outcome='exception',
        exc_info=True,
    )


def _sync_user_id(request):
    """The authenticated user's primary key, or None. Never a username."""
    user = getattr(request, 'user', None)
    if user is None:
        return None
    try:
        return user.pk if user.is_authenticated else None
    except Exception:  # noqa: BLE001 - a broken session must not break logging
        return None


async def _async_user_id(request):
    if not hasattr(request, 'auser'):
        return None
    try:
        user = await request.auser()
        return user.pk if user.is_authenticated else None
    except Exception:  # noqa: BLE001 - a broken session must not break logging
        return None


@sync_and_async_middleware
def RequestLoggingMiddleware(get_response):  # noqa: N802 - a middleware factory
    """Bind request context, expose `X-Request-ID`, log what deserves a line.

    Must sit **after** `AuthenticationMiddleware` so the user is already
    resolvable; otherwise every line would report an anonymous user.

    The incoming `X-Request-ID` header is deliberately ignored: a client-chosen
    value could collide with another request's id on purpose, making the log
    unusable exactly when it matters. The id is always generated here and
    returned in the response so an operator can quote it.
    """

    if iscoroutinefunction(get_response):

        async def middleware(request):
            request_id = new_request_id()
            request.request_id = request_id
            user_id = None if _is_excluded(request.path) else await _async_user_id(request)
            tokens = set_request_context(request_id=request_id, user_id=user_id)
            started = time.monotonic()
            try:
                response = await get_response(request)
            except Exception:
                _log_exception(
                    request, (time.monotonic() - started) * 1000, request_id, user_id
                )
                raise
            else:
                duration_ms = (time.monotonic() - started) * 1000
                response[REQUEST_ID_HEADER] = request_id
                _log_request(request, response, duration_ms, request_id, user_id)
                return response
            finally:
                # Always, so a later request in this worker can never inherit
                # the identity of this one.
                reset_request_context(tokens)

        return middleware

    def middleware(request):
        request_id = new_request_id()
        request.request_id = request_id
        # Resolving `request.user` forces the session lookup, so it is skipped
        # for the excluded paths, which are unauthenticated anyway.
        user_id = None if _is_excluded(request.path) else _sync_user_id(request)
        tokens = set_request_context(request_id=request_id, user_id=user_id)
        started = time.monotonic()
        try:
            response = get_response(request)
        except Exception:
            _log_exception(request, (time.monotonic() - started) * 1000, request_id, user_id)
            raise
        else:
            duration_ms = (time.monotonic() - started) * 1000
            response[REQUEST_ID_HEADER] = request_id
            _log_request(request, response, duration_ms, request_id, user_id)
            return response
        finally:
            reset_request_context(tokens)

    return middleware
