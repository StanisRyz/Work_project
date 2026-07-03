from django.contrib import admin

from .models import ActStatus, DefectType, Operation, Priority, TaskStatus


@admin.register(Operation)
class OperationAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'sort_order', 'is_active', 'updated_at')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)
    ordering = ('sort_order', 'name')


@admin.register(DefectType)
class DefectTypeAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'updated_at')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)
    ordering = ('name',)


@admin.register(ActStatus)
class ActStatusAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'sort_order', 'is_final', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_final', 'is_active')
    ordering = ('sort_order', 'name')


@admin.register(TaskStatus)
class TaskStatusAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'sort_order', 'is_final', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_final', 'is_active')
    ordering = ('sort_order', 'name')


@admin.register(Priority)
class PriorityAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'sort_order', 'color', 'is_active')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)
    ordering = ('sort_order', 'name')
