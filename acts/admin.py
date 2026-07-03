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
