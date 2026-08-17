from django.contrib import admin

from .models import WindingEntry
from .permissions import can_manage_workup


@admin.register(WindingEntry)
class WindingEntryAdmin(admin.ModelAdmin):
    """Read-only diagnostics unless the staff member is also ПДО.

    Admin is not a second door into «Проработка»: it reuses the very same
    `can_manage_workup()` the JSON endpoints do, so there is one definition of
    who owns the journal. Viewing and searching keep the ordinary Admin access
    model.
    """

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

    def has_add_permission(self, request):
        return super().has_add_permission(request) and can_manage_workup(request.user)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and can_manage_workup(request.user)

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and can_manage_workup(request.user)
