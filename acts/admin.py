from django.contrib import admin

from .models import Act, ActAttachment, ActComment, ActDefect, ActHistoryEvent


class ActDefectInline(admin.TabularInline):
    model = ActDefect
    extra = 0
    fields = ('znp_number', 'party_number', 'defect_type', 'operation', 'mp_type', 'checked_quantity', 'nonconforming_quantity', 'description', 'detected_at')


@admin.register(Act)
class ActAdmin(admin.ModelAdmin):
    inlines = (ActDefectInline,)
    list_display = (
        'number',
        'status',
        'customer',
        'order_number',
        'znp_number',
        'party_number',
        'nomenclature',
        'operation',
        'defect_type',
        'priority',
        'created_by',
        'due_date',
        'created_at',
    )
    search_fields = ('number', 'customer', 'order_number', 'znp_number', 'party_number', 'nomenclature', 'description')
    list_filter = ('status', 'operation', 'defect_type', 'priority', 'created_at')
    readonly_fields = ('created_at', 'updated_at', 'ko_decision_at', 'to_analysis_at', 'closed_at')
    fieldsets = (
        (
            None,
            {
                'fields': (
                    'number',
                    'created_by',
                    'customer',
                    'order_number',
                    'znp_number',
                    'party_number',
                    'nomenclature',
                    'kd_designation',
                    'operation',
                    'defect_type',
                    'priority',
                    'status',
                    'description',
                    'due_date',
                )
            },
        ),
        (
            'Решение КО',
            {
                'fields': (
                    'ko_decision',
                    'ko_comment',
                    'ko_decision_by',
                    'ko_decision_at',
                )
            },
        ),
        (
            'Анализ ТО',
            {
                'fields': (
                    'to_root_cause',
                    'to_action_summary',
                    'to_analysis_by',
                    'to_analysis_at',
                )
            },
        ),
        (
            'Закрытие',
            {
                'fields': (
                    'closed_by',
                    'closed_at',
                    'closing_comment',
                )
            },
        ),
        (
            'Служебные поля',
            {
                'fields': (
                    'created_at',
                    'updated_at',
                )
            },
        ),
    )


@admin.register(ActDefect)
class ActDefectAdmin(admin.ModelAdmin):
    list_display = ('act', 'znp_number', 'party_number', 'defect_type', 'operation', 'mp_type', 'checked_quantity', 'nonconforming_quantity', 'detected_at', 'created_at')
    search_fields = ('act__number', 'description', 'defect_type__name')
    list_filter = ('defect_type', 'detected_at', 'created_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ActHistoryEvent)
class ActHistoryEventAdmin(admin.ModelAdmin):
    list_display = ('act', 'event_type', 'user', 'from_status', 'to_status', 'created_at')
    search_fields = ('act__number', 'message', 'user__username')
    list_filter = ('event_type', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(ActComment)
class ActCommentAdmin(admin.ModelAdmin):
    list_display = ('act', 'author', 'short_text', 'created_at', 'updated_at')
    search_fields = ('act__number', 'text', 'author__username')
    list_filter = ('created_at', 'updated_at')
    readonly_fields = ('created_at', 'updated_at')

    def short_text(self, obj):
        return obj.text[:80]

    short_text.short_description = 'Комментарий'


@admin.register(ActAttachment)
class ActAttachmentAdmin(admin.ModelAdmin):
    list_display = (
        'act',
        'original_name',
        'uploaded_by',
        'file_size',
        'content_type',
        'uploaded_at',
    )
    search_fields = ('act__number', 'original_name', 'description', 'uploaded_by__username')
    list_filter = ('uploaded_at', 'content_type')
    readonly_fields = ('file_size', 'content_type', 'uploaded_at')
