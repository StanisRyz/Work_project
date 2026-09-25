"""The СМК records, read in Django Admin.

Read-only, like every other business record: a record, its findings, its
measures and their исполнители are written only by `smk/services.py`, which
creates, cancels and reissues the real tasks, notifies and writes the history
in the same transaction. An Admin edit would change a measure without touching
the task somebody is holding, and a delete would take the history with it.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import (
    SmkActionAssignee,
    SmkCorrectiveAction,
    SmkHistoryEvent,
    SmkNonConformity,
    SmkSource,
)


class SmkNonConformityInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = SmkNonConformity
    extra = 0


class SmkCorrectiveActionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = SmkCorrectiveAction
    extra = 0


@admin.register(SmkSource)
class SmkSourceAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'origin', 'status', 'created_by', 'created_at')
    list_filter = ('origin', 'status')
    inlines = (SmkNonConformityInline, SmkCorrectiveActionInline)


class SmkActionAssigneeInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = SmkActionAssignee
    extra = 0


@admin.register(SmkCorrectiveAction)
class SmkCorrectiveActionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'source', 'department', 'due_date')
    list_filter = ('department',)
    inlines = (SmkActionAssigneeInline,)


@admin.register(SmkHistoryEvent)
class SmkHistoryEventAdmin(admin.ModelAdmin):
    """Read-only: the trail is written by `smk/services.py` and by nothing else.

    Registered so an administrator can *look* at it — every field is listed as
    readonly and adding a row by hand is refused, because an event Admin wrote
    would claim a change that never happened.
    """

    list_display = ('pk', 'source', 'event_type', 'actor', 'created_at')
    list_filter = ('event_type',)
    readonly_fields = ('source', 'actor', 'event_type', 'message', 'created_at')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
