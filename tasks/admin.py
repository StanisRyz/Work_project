from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import Task, TaskAssignee


@admin.register(Task)
class TaskAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """A diagnostic view of tasks, not a second way to run the workflow.

    The source columns are here so a support question — *where did this task
    come from?* — is answerable without a shell. Everything stays read-only:
    tasks are created and completed by services, never in Admin.
    """

    list_display = (
        'id', 'source_type', 'source_reference', 'status', 'task_text',
        'department', 'due_date', 'created_at',
    )
    search_fields = ('task_text', 'act__number', 'assignees__user__username')
    list_filter = ('source_type', 'status', 'department', 'due_date', 'created_at')
    readonly_fields = ('created_at',)

    @admin.display(description='Источник')
    def source_reference(self, obj):
        """The one relation that actually identifies this task's origin."""
        if obj.source_type == Task.SourceType.ACT:
            return obj.act
        if obj.source_type == Task.SourceType.PROTOCOL_ACTION:
            return obj.protocol_action
        return obj.protocol


@admin.register(TaskAssignee)
class TaskAssigneeAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('task', 'user')
    search_fields = ('task__task_text', 'user__username')
