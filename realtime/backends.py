"""Publisher backends.

`Noop` remains the default and sends nothing. `Capture` and `Failing` exist for
tests. `Redis` is the RT-2 transport — adding it required no change to any
business service, which is the point of the publisher abstraction.
"""

import logging
from abc import ABC, abstractmethod

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
    """Publishes one serialized event into every target's Pub/Sub channel.

    Runs inside the `on_commit` callback of a user request, so it must be
    quick: the client is built from a shared pool with short socket timeouts
    and performs no retry loop of its own. A failure is raised as
    :class:`RealtimePublisherError` and handled by the existing
    `REALTIME_FAIL_SILENTLY` policy in `realtime.publisher.dispatch_event`.

    Redis is a short-lived transport here, never a store: a message nobody is
    subscribed to is simply dropped, which is not an error.
    """

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
        channels = channels_for_targets(normalize_targets(targets), prefix)
        if not channels:
            return None

        client = self._client()
        delivered = 0
        try:
            for channel in channels:
                # A zero return value means nobody is listening right now; that
                # is a normal state for a transport, not a failure.
                delivered += int(client.publish(channel, payload) or 0)
        except redis_exception_types() as exc:
            raise RealtimePublisherError(
                f'Не удалось опубликовать событие в Redis: {describe_failure(exc)}'
            ) from exc

        logger.debug(
            'realtime published to redis: event_id=%(event_id)s event_type=%(event_type)s '
            'channels=%(channels)d subscribers=%(subscribers)d',
            {**event.log_context(), 'channels': len(channels), 'subscribers': delivered},
        )
        return None
