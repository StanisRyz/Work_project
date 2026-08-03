from .models import Notification


def notification_summary(request):
    if not request.user.is_authenticated:
        return {'notification_unread_count': 0, 'recent_notifications': ()}
    unread_notifications = Notification.objects.filter(recipient=request.user, is_read=False)
    return {
        'notification_unread_count': unread_notifications.count(),
        'recent_notifications': unread_notifications.select_related('actor', 'related_act')[:5],
    }
