from django.contrib import admin

from .models import Task


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ('id', 'status', 'task_text', 'act', 'department', 'responsible', 'due_date', 'created_at')
    search_fields = ('task_text', 'act__number', 'responsible__username')
    list_filter = ('status', 'department', 'due_date', 'created_at')
    readonly_fields = ('created_at',)
