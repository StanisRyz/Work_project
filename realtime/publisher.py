"""Publisher selection, after-commit dispatch and failure handling.

Business code calls :func:`publish_after_commit` (usually indirectly, through
:mod:`realtime.emitters`) and never imports a transport client. Which backend
actually runs is a settings decision resolved by dotted path.
"""

import logging

from django.conf import settings
from django.core.signals import setting_changed
from django.db import transaction
from django.dispatch import receiver
from django.utils.module_loading import import_string

from .targets import normalize_targets


logger = logging.getLogger('realtime')

DEFAULT_BACKEND = 'realtime.backends.NoopRealtimePublisher'

_publisher = None
_override = None


def realtime_enabled():
    return bool(getattr(settings, 'REALTIME_ENABLED', False))


def fail_silently():
    return bool(getattr(settings, 'REALTIME_FAIL_SILENTLY', True))


def get_publisher():
    """Return the configured backend instance, loading it at most once."""
    global _publisher
    if _override is not None:
        return _override
    if _publisher is None:
        dotted_path = getattr(settings, 'REALTIME_PUBLISHER_BACKEND', DEFAULT_BACKEND)
        _publisher = import_string(dotted_path)()
    return _publisher


def set_publisher(publisher):
    """Force a backend instance. Intended for tests and local diagnostics."""
    global _override
    _override = publisher
    return publisher


def reset_publisher():
    """Drop both the override and the cached instance."""
    global _publisher, _override
    _publisher = None
    _override = None


@receiver(setting_changed)
def _reset_publisher_on_setting_change(sender, setting, **kwargs):
    # `override_settings(REALTIME_PUBLISHER_BACKEND=...)` must actually take
    # effect, so the cached instance is dropped when the setting moves.
    if setting == 'REALTIME_PUBLISHER_BACKEND':
        global _publisher
        _publisher = None


def dispatch_event(event, targets):
    """Publish one event immediately. Returns whether it reached the backend.

    A backend failure never rolls anything back: by the time this runs the
    business transaction is already committed, so the only choices are to log
    and continue (the default) or to let the exception surface for diagnosis.
    """
    if not realtime_enabled():
        return False
    targets = normalize_targets(targets)
    if not targets:
        return False

    publisher = get_publisher()
    context = {**event.log_context(), 'targets': len(targets), 'backend': publisher.label}
    try:
        publisher.publish(event, targets)
    except Exception as exc:  # noqa: BLE001 - the transport boundary is deliberate
        logger.error(
            'realtime publish failed: event_id=%(event_id)s event_type=%(event_type)s '
            'resource=%(resource_type)s:%(resource_id)s targets=%(targets)d '
            'backend=%(backend)s exception=%(exception)s',
            {**context, 'exception': type(exc).__name__},
            exc_info=not fail_silently(),
        )
        if not fail_silently():
            raise
        return False

    logger.debug(
        'realtime published: event_id=%(event_id)s event_type=%(event_type)s '
        'resource=%(resource_type)s:%(resource_id)s targets=%(targets)d backend=%(backend)s',
        context,
    )
    return True


def publish_after_commit(event, targets, using=None):
    """Register `event` for publication once the current transaction commits.

    Targets are normalized here, inside the transaction, so an invalid target
    fails loudly at the call site instead of inside a commit hook. A rollback
    means the callback never runs and nothing is published.
    """
    if not realtime_enabled():
        return False
    targets = normalize_targets(targets)
    if not targets:
        return False
    transaction.on_commit(lambda: dispatch_event(event, targets), using=using)
    return True
