from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    """Technical app for the transport-independent real-time event contract.

    It deliberately defines no models and therefore has no migrations, and it
    registers no URLs, views or templates. It holds only the event contract,
    targets, the publisher abstraction, its backends and their tests.

    No signal receivers are connected here on purpose: real-time events are
    published explicitly from the business services, never from generic
    `post_save` handlers (see docs/realtime.md).
    """

    name = 'realtime'
    verbose_name = 'Real-time события'

    def ready(self):
        # System checks only — still no signal receivers: events are published
        # explicitly from the business services.
        from . import checks  # noqa: F401
