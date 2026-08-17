from django.contrib import admin

from .models import WindingEntry


@admin.register(WindingEntry)
class WindingEntryAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'd', 'outer_diameter', 'b', 'tape_thickness_mm', 'complexity_coefficient',
        'source', 'batch_quantity', 'actual_unit_time_hours', 'production_confirmed',
    )
    list_filter = ('source', 'production_confirmed', 'calibration_enabled', 'calculation_version')
    search_fields = ('name', 'case_key', 'employee_name')
    # Server-derived: never edited by hand, or the audit trail stops matching
    # the calculation that produced the row.
    readonly_fields = (
        'case_key', 'calculation_signature', 'actual_unit_time_hours',
        'one_c_hours', 'created_at', 'updated_at',
    )
