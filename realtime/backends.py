"""Publisher backends.

`Noop` remains the default and sends nothing. `Capture` and `Failing` exist for
tests. `Redis` is the RT-2 transport — adding it required no change to any
business service, which is the point of the publisher abstraction.
"""

import logging
import time
from abc import ABC, abstractmethod

from django.conf import settings

from .channels import channels_for_targets, normalize_channel_prefix
from .targets import normalize_targets


logger = logging.getLogger('realtime')


class RealtimePublisherError(Exception):
    """A backend failed to hand the event to its transport."""


class RealtimePublisher(ABC):
    """The single interface every backend implements."""

    @abstractmethod
    def publish(self, event, targets):
        """Hand one event to the transport for the given targets.

        `targets` is always an already normalized tuple of
        :class:`~realtime.targets.RealtimeTarget`.
        """

    @property
    def label(self):
        return f'{type(self).__module__}.{type(self).__qualname__}'


class NoopRealtimePublisher(RealtimePublisher):
    """The default: accepts everything, sends nothing.

    With this backend the project behaves exactly as it did before real-time
    events existed.
    """

    def publish(self, event, targets):
        return None


class CaptureRealtimePublisher(RealtimePublisher):
    """Keeps every published event and its targets, for tests."""

    def __init__(self):
        self.published = []

    def publish(self, event, targets):
        self.published.append((event, tuple(targets)))

    def clear(self):
        self.published.clear()

    @property
    def events(self):
        return [event for event, _targets in self.published]

    def events_of_type(self, event_type):
        return [event for event in self.events if event.event_type == event_type]

    def targets_of_type(self, event_type):
        return [
            targets
            for event, targets in self.published
            if event.event_type == event_type
        ]


class FailingRealtimePublisher(RealtimePublisher):
    """Always raises, so resilience can be verified deliberately."""

    message = 'Real-time backend недоступен (тестовый сбой).'

    def publish(self, event, targets):
        raise RealtimePublisherError(self.message)


class RedisRealtimePublisher(RealtimePublisher):
    """Publishes one serialized event into every subscribable Pub/Sub channel.

    Runs inside the `on_commit` callback of a user request, so it must be
    quick: the client is built from a shared pool with short socket timeouts,
    every channel is written in a single pipelined round trip, and there is no
    retry loop of its own. A failure is raised as
    :class:`RealtimePublisherError` and handled by the existing
    `REALTIME_FAIL_SILENTLY` policy in `realtime.publisher.dispatch_event`.

    Redis is a short-lived transport here, never a store: a message nobody is
    subscribed to is simply dropped, which is not an error.

    Only targets a client can actually *subscribe* to are materialised as Redis
    channels — today that means `user:<id>` only. `act:<id>` remains a valid
    routing hint in the event contract, but publishing it would write messages
    nobody can ever receive: the browser is not allowed to subscribe to an act
    room until that room is authorised through `acts.permissions.can_view_act`.
    """

    # Target kinds a subscriber may exist for. Extending this is what makes an
    # act room real, and it must happen together with the authorised
    # subscription, not before it.
    SUBSCRIBABLE_KINDS = frozenset({'user'})

    def __init__(self, client_factory=None, prefix=None):
        # Injectable for tests; production leaves both at None and uses the
        # pooled client from `realtime.transport`.
        self._client_factory = client_factory
        self._prefix = prefix

    def _client(self):
        if self._client_factory is not None:
            return self._client_factory()
        from .transport import sync_client  # noqa: PLC0415 - lazy, keeps redis optional

        return sync_client()

    def publish(self, event, targets):
        from .transport import (  # noqa: PLC0415 - lazy, keeps redis optional
            describe_failure,
            get_max_event_bytes,
            redis_exception_types,
        )

        # Serialized once, then sent unchanged to every channel.
        payload = event.as_compact_json().encode('utf-8')
        max_bytes = get_max_event_bytes()
        if len(payload) > max_bytes:
            logger.warning(
                'realtime event dropped before publish: event_id=%(event_id)s '
                'event_type=%(event_type)s resource=%(resource_type)s:%(resource_id)s '
                'bytes=%(bytes)d limit=%(limit)d',
                {**event.log_context(), 'bytes': len(payload), 'limit': max_bytes},
            )
            return None

        prefix = normalize_channel_prefix(self._prefix)
        # `dispatch_event` already normalizes, but doing it again here makes the
        # channel list deterministic for any caller, not just that one.
        subscribable = [
            target
            for target in normalize_targets(targets)
            if target.kind in self.SUBSCRIBABLE_KINDS
        ]
        channels = channels_for_targets(subscribable, prefix)
        if not channels:
            return None

        client = self._client()
        started = time.monotonic()
        try:
            # One network round trip for every channel. `transaction=False`
            # because these are independent fire-and-forget PUBLISH commands:
            # MULTI/EXEC would add two commands and atomicity guarantees that
            # mean nothing for a best-effort transport.
            pipeline = client.pipeline(transaction=False)
            for channel in channels:
                pipeline.publish(channel, payload)
            results = pipeline.execute()
        except redis_exception_types() as exc:
            raise RealtimePublisherError(
                f'Не удалось опубликовать событие в Redis: {describe_failure(exc)}'
            ) from exc
        duration_ms = (time.monotonic() - started) * 1000

        # A zero return value means nobody is listening right now; that is a
        # normal state for a transport, not a failure.
        delivered = sum(int(result or 0) for result in results or ())

        slow_after_ms = float(getattr(settings, 'REALTIME_REDIS_SLOW_PUBLISH_MS', 250.0))
        context = {
            **event.log_context(),
            'channels': len(channels),
            'subscribers': delivered,
            'duration_ms': int(duration_ms),
        }
        if duration_ms >= slow_after_ms:
            # Never the payload, the Redis URL or any credential: only the
            # event type, how many channels were written and how long it took.
            logger.warning(
                'realtime.slow_publish event_type=%(event_type)s channels=%(channels)d '
                'duration_ms=%(duration_ms)d',
                context,
            )
        else:
            logger.debug(
                'realtime published to redis: event_id=%(event_id)s event_type=%(event_type)s '
                'channels=%(channels)d subscribers=%(subscribers)d duration_ms=%(duration_ms)d',
                context,
            )
        return None
