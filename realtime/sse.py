"""Server-Sent Events: frame formatting and the async Redis subscriber.

The stream is deliberately dumb. It forwards validated events and heartbeats,
and it drops anything it cannot trust — a malformed, unknown or oversized
message never reaches the client and never tears the connection down.

Nothing about routing is taken from the client: the caller passes a user id
that came from the Django session, and this module derives the one channel that
user may listen to.
"""

import asyncio
import logging
import time
import uuid

from django.conf import settings

from ecosystem.logging_utils import log_event

from .channels import user_channel
from .events import RealtimeEvent, RealtimeEventError


logger = logging.getLogger('realtime')

FRAME_END = '\n\n'
HEARTBEAT_COMMENT = 'heartbeat'


# --------------------------------------------------------------------------
# Frame formatting
# --------------------------------------------------------------------------

def format_retry(delay_ms=None):
    """The initial frame telling the client how long to wait before retrying."""
    if delay_ms is None:
        delay_ms = getattr(settings, 'REALTIME_RECONNECT_DELAY_MS', 3000)
    return f'retry: {int(delay_ms)}{FRAME_END}'


def format_event(event):
    """One business event as an SSE frame.

    `data` is the compact single-line payload from the event contract, so it
    contains neither targets nor the Redis channel name.
    """
    return (
        f'id: {event.event_id}\n'
        f'event: {event.event_type.value}\n'
        f'data: {event.as_compact_json()}'
        f'{FRAME_END}'
    )


def format_heartbeat(comment=HEARTBEAT_COMMENT):
    """An SSE comment: keeps proxies and clients from treating silence as death."""
    text = str(comment).replace('\n', ' ').replace('\r', ' ')
    return f': {text}{FRAME_END}'


# --------------------------------------------------------------------------
# Message decoding
# --------------------------------------------------------------------------

def decode_message(raw, *, max_bytes=None):
    """Return a validated event, or None when the message must be dropped."""
    if max_bytes is None:
        max_bytes = int(getattr(settings, 'REALTIME_MAX_EVENT_BYTES', 16384))
    try:
        return RealtimeEvent.from_json(raw, max_bytes=max_bytes)
    except RealtimeEventError as exc:
        # Logged without the payload: a broken message may contain anything.
        logger.warning('realtime stream dropped a message: %s', exc)
        return None


# --------------------------------------------------------------------------
# Subscriber
# --------------------------------------------------------------------------

async def _close_quietly(subscription, channel):
    """Release the PubSub and its client, whatever state they are in."""
    pubsub = subscription.get('pubsub')
    client = subscription.get('client')
    if pubsub is not None:
        try:
            await pubsub.unsubscribe(channel)
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.debug('realtime unsubscribe failed during cleanup: %s', type(exc).__name__)
        try:
            await pubsub.aclose()
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.debug('realtime pubsub close failed during cleanup: %s', type(exc).__name__)
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001 - teardown must never raise
            logger.debug('realtime client close failed during cleanup: %s', type(exc).__name__)


async def event_stream(user_id, *, client_factory=None, max_messages=None, max_lifetime=None):
    """Yield SSE frames for one authenticated user until they disconnect.

    The stream also ends by itself after `REALTIME_MAX_CONNECTION_SECONDS`, so
    a long-lived connection cannot outlive its session: the browser reconnects
    on its own and the new request re-authenticates and re-checks permissions
    from scratch. Heartbeats continue right up to that point.

    `client_factory`, `max_messages` and `max_lifetime` exist so tests can drive
    the loop deterministically; production passes none of them.
    """
    from .transport import (  # noqa: PLC0415 - lazy, keeps redis optional
        async_client,
        describe_failure,
        redis_exception_types,
    )

    channel = user_channel(user_id)
    heartbeat = float(getattr(settings, 'REALTIME_HEARTBEAT_SECONDS', 25.0))
    max_bytes = int(getattr(settings, 'REALTIME_MAX_EVENT_BYTES', 16384))
    if max_lifetime is None:
        max_lifetime = float(getattr(settings, 'REALTIME_MAX_CONNECTION_SECONDS', 900.0))
    connection_id = uuid.uuid4().hex[:12]
    started = time.monotonic()
    subscription = {}
    delivered = 0
    events_sent = 0
    reason = 'client_disconnect'

    yield format_retry()

    try:
        client = (client_factory or async_client)()
        subscription['client'] = client
        pubsub = client.pubsub(ignore_subscribe_messages=True)
        subscription['pubsub'] = pubsub
        await pubsub.subscribe(channel)
        # The channel name is never logged: it is built from the user target
        # and belongs to the transport, not to the diagnostic record.
        log_event(
            logger,
            'INFO',
            'realtime.connection_opened',
            connection_id=connection_id,
            user_id=user_id,
            max_lifetime_s=float(max_lifetime),
        )

        while True:
            if max_lifetime and (time.monotonic() - started) >= max_lifetime:
                # Controlled end of life: no error, the client just reconnects.
                reason = 'max_lifetime'
                return
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=heartbeat
            )
            if message is None:
                # Silence for a whole heartbeat interval, not a dead stream.
                yield format_heartbeat()
                if max_messages is not None:
                    delivered += 1
                    if delivered >= max_messages:
                        reason = 'test_limit'
                        return
                continue
            # Subscribe/unsubscribe confirmations carry no payload.
            if message.get('type') != 'message':
                continue

            event = decode_message(message.get('data'), max_bytes=max_bytes)
            if event is None:
                # One bad message must not end the stream. The payload itself
                # is never logged — a malformed message may contain anything.
                log_event(
                    logger,
                    'WARNING',
                    'realtime.invalid_message',
                    connection_id=connection_id,
                    user_id=user_id,
                    outcome='dropped',
                )
                continue
            yield format_event(event)
            events_sent += 1
            if max_messages is not None:
                delivered += 1
                if delivered >= max_messages:
                    reason = 'test_limit'
                    return
    except asyncio.CancelledError:
        # A closed browser tab is ordinary, not an incident.
        reason = 'cancelled'
        log_event(
            logger,
            'DEBUG',
            'realtime.connection_cancelled',
            connection_id=connection_id,
            user_id=user_id,
        )
        raise
    except redis_exception_types() as exc:
        # An unexpected Redis disconnect ends this stream in a controlled way;
        # the client reconnects after the advertised retry delay.
        reason = 'redis_disconnected'
        log_event(
            logger,
            'WARNING',
            'realtime.redis_disconnected',
            connection_id=connection_id,
            user_id=user_id,
            error_type=type(exc).__name__,
            # `describe_failure` keeps the host visible for diagnosis but
            # strips the URL, username and password.
            detail=describe_failure(exc),
            outcome='disconnected',
        )
    finally:
        await _close_quietly(subscription, channel)
        log_event(
            logger,
            'INFO',
            'realtime.connection_closed',
            connection_id=connection_id,
            user_id=user_id,
            duration_ms=(time.monotonic() - started) * 1000,
            event_count=events_sent,
            reason=reason,
        )


async def redis_is_reachable(*, client_factory=None):
    """Short connectivity probe run before the stream is opened."""
    from .transport import (  # noqa: PLC0415 - lazy, keeps redis optional
        async_client,
        describe_failure,
        get_connect_timeout,
        redis_exception_types,
    )

    client = None
    try:
        client = (client_factory or async_client)()
        await asyncio.wait_for(client.ping(), timeout=get_connect_timeout())
        return True, ''
    except asyncio.TimeoutError:
        return False, 'Redis не ответил на PING за отведённое время.'
    except redis_exception_types() as exc:
        return False, describe_failure(exc)
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - teardown must never raise
                logger.debug('realtime probe close failed: %s', type(exc).__name__)
