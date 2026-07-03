from django.contrib import admin

from .models import Department, UserProfile


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'updated_at')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'department', 'position', 'is_active')
    search_fields = (
        'user__username',
        'user__first_name',
        'user__last_name',
        'department__name',
    )
    list_filter = ('role', 'department', 'is_active')
