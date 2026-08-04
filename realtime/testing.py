"""Test helpers for the real-time contract.

Kept out of the production modules so business code never imports a test
backend by accident.
"""

from contextlib import contextmanager

from django.test import override_settings

from .backends import CaptureRealtimePublisher, FailingRealtimePublisher
from .publisher import reset_publisher, set_publisher


@contextmanager
def capture_realtime_events(fail_silently=True):
    """Turn real-time on with a capturing backend and yield it."""
    publisher = CaptureRealtimePublisher()
    with override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=fail_silently):
        set_publisher(publisher)
        try:
            yield publisher
        finally:
            reset_publisher()


@contextmanager
def failing_realtime_publisher(fail_silently=True):
    """Turn real-time on with a backend that always raises."""
    publisher = FailingRealtimePublisher()
    with override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=fail_silently):
        set_publisher(publisher)
        try:
            yield publisher
        finally:
            reset_publisher()
