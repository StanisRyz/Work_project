from django.contrib import admin

from .models import Notification, NotificationDelivery


class NotificationDeliveryInline(admin.TabularInline):
    model = NotificationDelivery
    extra = 0
    readonly_fields = (
        'channel',
        'status',
        'attempts',
        'available_at',
        'started_at',
        'last_attempt_at',
        'sent_at',
        'last_error',
        'created_at',
        'updated_at',
    )
    can_delete = False


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('title', 'recipient', 'event_type', 'related_act', 'created_at', 'is_read')
    list_filter = ('event_type', 'is_read', 'created_at')
    search_fields = ('title', 'message', 'recipient__username', 'related_act__number')
    raw_id_fields = ('recipient', 'actor', 'related_act')
    readonly_fields = ('created_at',)
    inlines = (NotificationDeliveryInline,)


@admin.register(NotificationDelivery)
class NotificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ('notification', 'channel', 'status', 'attempts', 'last_attempt_at', 'sent_at')
    list_filter = ('channel', 'status')
    search_fields = ('notification__title', 'notification__recipient__username', 'last_error')
    raw_id_fields = ('notification',)
    readonly_fields = ('created_at', 'updated_at')
