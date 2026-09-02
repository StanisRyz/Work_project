from django.contrib import admin

from .models import (
    SmkActionAssignee,
    SmkCorrectiveAction,
    SmkNonConformity,
    SmkSource,
)


class SmkNonConformityInline(admin.TabularInline):
    model = SmkNonConformity
    extra = 0


class SmkCorrectiveActionInline(admin.TabularInline):
    model = SmkCorrectiveAction
    extra = 0


@admin.register(SmkSource)
class SmkSourceAdmin(admin.ModelAdmin):
    list_display = ('pk', 'origin', 'created_by', 'created_at')
    list_filter = ('origin',)
    inlines = (SmkNonConformityInline, SmkCorrectiveActionInline)


class SmkActionAssigneeInline(admin.TabularInline):
    model = SmkActionAssignee
    extra = 0


@admin.register(SmkCorrectiveAction)
class SmkCorrectiveActionAdmin(admin.ModelAdmin):
    list_display = ('pk', 'source', 'department', 'due_date')
    list_filter = ('department',)
    inlines = (SmkActionAssigneeInline,)
