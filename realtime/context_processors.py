"""Client configuration for the real-time UI.

Only what the browser legitimately needs: whether real-time is on, reversed
URLs, timing values and an opaque tab-coordination epoch. Never a Redis URL,
channel name, credential, session key or user id — the SSE endpoint derives
the subscription from the session on its own.
"""

import secrets

from django.conf import settings
from django.urls import reverse


COORDINATION_EPOCH_SESSION_KEY = '_realtime_coordination_epoch'


def _coordination_epoch(request):
    epoch = request.session.get(COORDINATION_EPOCH_SESSION_KEY)
    if not isinstance(epoch, str) or len(epoch) < 20:
        # Browser coordination only: this is random per session, never an auth token.
        epoch = secrets.token_urlsafe(24)
        request.session[COORDINATION_EPOCH_SESSION_KEY] = epoch
    return epoch


def realtime_client_config(request):
    user = getattr(request, 'user', None)
    enabled = bool(getattr(settings, 'REALTIME_ENABLED', False)) and bool(
        getattr(user, 'is_authenticated', False)
    )
    if not enabled:
        return {'realtime_client': {'enabled': False}}
    return {
        'realtime_client': {
            'enabled': True,
            'coordination_epoch': _coordination_epoch(request),
            'events_url': reverse('realtime:events'),
            'sync_url': reverse('realtime:sync'),
            'notification_fragment_url': reverse('notifications:header_fragment'),
            'notifications_url': reverse('notifications:list'),
            # Plain numbers only. Nothing here describes Redis.
            'degraded_after_seconds': settings.REALTIME_DEGRADED_AFTER_SECONDS,
            'sync_poll_seconds': settings.REALTIME_SYNC_POLL_SECONDS,
            'sync_hidden_poll_seconds': settings.REALTIME_SYNC_HIDDEN_POLL_SECONDS,
            'leader_lease_seconds': settings.REALTIME_LEADER_LEASE_SECONDS,
            'leader_heartbeat_seconds': settings.REALTIME_LEADER_HEARTBEAT_SECONDS,
            'live_sync_seconds': settings.REALTIME_LIVE_SYNC_SECONDS,
        }
    }
