"""Test helpers for deployment configuration.

Kept out of the settings module so production code never imports test
machinery by accident, mirroring `realtime/testing.py`.
"""

from contextlib import contextmanager
from importlib import import_module, reload

from django.test import override_settings
from django.urls import clear_url_caches


# Reloaded innermost-first: `acts.urls` rebuilds its own list, then
# `ecosystem.urls` rebuilds the `include()` that points at it, then the
# resolver caches are dropped so the next reverse()/resolve() sees both.
URLCONF_MODULES = ('acts.urls', 'ecosystem.urls')


def reload_urlconf():
    """Rebuild the URLconf after a setting that gates a route has changed."""
    for name in URLCONF_MODULES:
        reload(import_module(name))
    clear_url_caches()


@contextmanager
def demo_reset_enabled(enabled=True):
    """Toggle `ENABLE_DEMO_RESET` and rebuild the URLconf around the block.

    The destructive reset route is registered conditionally at import time, so
    flipping the setting alone is not enough for `reverse()` to find it — or,
    on the way out, to stop finding it.
    """
    with override_settings(ENABLE_DEMO_RESET=enabled):
        reload_urlconf()
        try:
            yield
        finally:
            pass
    reload_urlconf()
