from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import DisposalAct, DisposalCommissionMember, DisposalItem


class DisposalItemInline(admin.TabularInline):
    model = DisposalItem
    extra = 0
    can_delete = False
    readonly_fields = (
        'defect', 'position', 'act_number', 'act_date', 'nomenclature', 'order_number',
        'znp_number', 'party_number', 'mp_type', 'defect_type', 'quantity',
    )

    def has_add_permission(self, request, obj=None):
        return False


class DisposalCommissionInline(admin.TabularInline):
    model = DisposalCommissionMember
    extra = 0
    can_delete = False
    readonly_fields = ('user', 'position', 'name', 'order')

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DisposalAct)
class DisposalActAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Diagnostics only: МП are disposed of on «Утилизация МП», never here."""

    list_display = ('id', 'disposed_by', 'disposed_at')
    list_filter = ('disposed_at',)
    inlines = (DisposalCommissionInline, DisposalItemInline)
