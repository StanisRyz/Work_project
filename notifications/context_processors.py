from .models import Notification


def notification_summary(request):
    if not request.user.is_authenticated:
        return {'notification_unread_count': 0, 'recent_notifications': ()}
    notifications = Notification.objects.filter(recipient=request.user)
    return {
        'notification_unread_count': notifications.filter(is_read=False).count(),
        'recent_notifications': notifications.select_related('actor', 'related_act')[:5],
    }
