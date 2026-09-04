from django.contrib import admin

from .models import Department, UserProfile


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_active', 'updated_at')
    search_fields = ('name', 'code')
    list_filter = ('is_active',)


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Roles, departments and who answers for bugs — Admin is the only place.

    `is_bug_responsible` is editable straight from the list, so marking or
    unmarking somebody is one click and a save rather than opening each
    profile: that column *is* the recipient list of «Сообщить об ошибке», and
    `bugs.permissions.get_bug_responsible_users()` reads it live, so a change
    here takes effect on the next report with nothing to restart.
    """

    list_display = (
        'user', 'role', 'department', 'position', 'is_active', 'is_bug_responsible',
    )
    list_editable = ('is_bug_responsible',)
    search_fields = (
        'user__username',
        'user__first_name',
        'user__last_name',
        'department__name',
    )
    list_filter = ('role', 'department', 'is_active', 'is_bug_responsible')
    actions = ('mark_bug_responsible', 'unmark_bug_responsible')

    @admin.action(description='Назначить ответственными за ошибки')
    def mark_bug_responsible(self, request, queryset):
        updated = queryset.update(is_bug_responsible=True)
        self.message_user(request, f'Назначено ответственных за ошибки: {updated}.')

    @admin.action(description='Снять ответственность за ошибки')
    def unmark_bug_responsible(self, request, queryset):
        updated = queryset.update(is_bug_responsible=False)
        self.message_user(request, f'Снята ответственность за ошибки: {updated}.')
