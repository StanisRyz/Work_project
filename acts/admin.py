from django.contrib import admin

from .models import Act


@admin.register(Act)
class ActAdmin(admin.ModelAdmin):
    list_display = (
        'number',
        'status',
        'party_number',
        'nomenclature',
        'operation',
        'defect_type',
        'priority',
        'created_by',
        'due_date',
        'created_at',
    )
    search_fields = ('number', 'party_number', 'nomenclature', 'description')
    list_filter = ('status', 'operation', 'defect_type', 'priority', 'created_at')
    readonly_fields = ('created_at', 'updated_at', 'ko_decision_at', 'to_analysis_at')
    fieldsets = (
        (
            None,
            {
                'fields': (
                    'number',
                    'created_by',
                    'party_number',
                    'nomenclature',
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
            'Служебные поля',
            {
                'fields': (
                    'created_at',
                    'updated_at',
                )
            },
        ),
    )
