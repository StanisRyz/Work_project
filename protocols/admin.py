"""Development and diagnostic views of the protocol tables.

`ProtocolType` is reference data and stays editable, like the rows in
`references`. Everything else is a business record: read-only here, changed
only through `protocols/services.py`. This is not the future workflow UI.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import (
    Protocol,
    ProtocolAction,
    ProtocolAgendaItem,
    ProtocolHistoryEvent,
    ProtocolParticipant,
    ProtocolSpeech,
    ProtocolType,
)


@admin.register(ProtocolType)
class ProtocolTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'display_order', 'is_active', 'updated_at')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)
    ordering = ('display_order', 'name')


class ProtocolParticipantInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ProtocolParticipant
    extra = 0
    fields = ('display_order', 'user', 'display_name', 'position', 'department_name', 'requires_approval')


class ProtocolAgendaItemInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ProtocolAgendaItem
    extra = 0
    fields = ('display_order', 'text')


class ProtocolActionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = ProtocolAction
    extra = 0
    fields = ('display_order', 'task_text', 'department', 'due_date')


@admin.register(Protocol)
class ProtocolAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    inlines = (ProtocolParticipantInline, ProtocolAgendaItemInline, ProtocolActionInline)
    list_display = ('protocol_type', 'number', 'status', 'revision', 'author', 'created_at')
    search_fields = ('number', 'author__username')
    list_filter = ('protocol_type', 'status', 'created_at')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(ProtocolSpeech)
class ProtocolSpeechAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('protocol', 'speaker', 'display_order')
    search_fields = ('text', 'speaker__display_name')
    list_filter = ('protocol__protocol_type',)


@admin.register(ProtocolHistoryEvent)
class ProtocolHistoryEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('protocol', 'event_type', 'revision', 'actor', 'created_at')
    search_fields = ('message', 'actor__username')
    list_filter = ('event_type', 'created_at')
    readonly_fields = ('created_at',)
