"""Publisher backends.

RT-1 ships no network transport. These three backends cover the default
behaviour and the two behaviours the tests need; a Redis or SSE backend is an
RT-2 concern and must be added here without touching any business service.
"""

from abc import ABC, abstractmethod


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
