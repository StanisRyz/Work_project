"""Client configuration for the real-time UI.

Only what the browser legitimately needs: whether real-time is on and three
reversed URLs. Never a Redis URL, a channel name, credentials or a user id —
the SSE endpoint derives the subscription from the session on its own.
"""

from django.conf import settings
from django.urls import reverse


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
            'events_url': reverse('realtime:events'),
            'notification_fragment_url': reverse('notifications:header_fragment'),
            'notifications_url': reverse('notifications:list'),
        }
    }
