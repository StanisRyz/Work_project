from .services import get_notification_header_state


def notification_summary(request):
    """Bell state for every rendered page.

    Delegates to the shared service so a full page load and a real-time
    fragment refresh always describe the same thing.
    """
    state = get_notification_header_state(request.user)
    return {
        'notification_unread_count': state['unread_count'],
        'recent_notifications': state['items'],
    }
