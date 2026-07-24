from django.contrib import admin

from .models import Task, TaskAssignee


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'task_text', 'act', 'department', 'due_date', 'created_at')
    search_fields = ('task_text', 'act__number', 'assignees__user__username')
    list_filter = ('status', 'department', 'due_date', 'created_at')
    readonly_fields = ('created_at',)


@admin.register(TaskAssignee)
class TaskAssigneeAdmin(admin.ModelAdmin):
    list_display = ('task', 'user')
    search_fields = ('task__task_text', 'user__username')
