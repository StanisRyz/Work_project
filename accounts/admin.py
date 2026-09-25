from django.contrib import admin
from django.db import transaction
from django.utils import timezone

from .models import Department, RoleSubstitution, UserProfile


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


class ActiveSubstitutionFilter(admin.SimpleListFilter):
    """«Действует сейчас» / «Запланировано» / «Завершено», by today's date."""

    title = 'Период'
    parameter_name = 'period'

    def lookups(self, request, model_admin):
        return (
            ('active', 'Действует сейчас'),
            ('planned', 'Запланировано'),
            ('finished', 'Завершено'),
        )

    def queryset(self, request, queryset):
        today = timezone.localdate()
        if self.value() == 'active':
            return queryset.filter(date_from__lte=today, date_to__gte=today)
        if self.value() == 'planned':
            return queryset.filter(date_from__gt=today)
        if self.value() == 'finished':
            return queryset.filter(date_to__lt=today)
        return queryset


@admin.register(RoleSubstitution)
class RoleSubstitutionAdmin(admin.ModelAdmin):
    """«Замещения»: a role lent to an employee for a period, on top of their own.

    The only place one is set. Every permission reads it through
    `accounts.roles`, so saving here takes effect on the user's next request
    and ends by itself after «По». Saving one that is in force today also puts
    the substitute on the act-stage entries of «Задачи» their lent role
    already has (`tasks.services.add_substitute_to_open_act_workflow_tasks`);
    personal tasks of the person replaced never move.
    """

    list_display = (
        'user', 'role', 'substitutes_for', 'date_from', 'date_to', 'is_active_now', 'reason',
        'created_by',
    )
    list_filter = (ActiveSubstitutionFilter, 'role')
    search_fields = (
        'user__username', 'user__first_name', 'user__last_name',
        'substitutes_for__username', 'substitutes_for__last_name',
    )
    autocomplete_fields = ('user', 'substitutes_for')
    fields = ('user', 'role', 'substitutes_for', 'date_from', 'date_to', 'reason',
              'created_by', 'created_at')
    readonly_fields = ('created_by', 'created_at')
    list_select_related = ('user', 'substitutes_for', 'created_by')
    date_hierarchy = 'date_from'

    @admin.display(boolean=True, description='Действует')
    def is_active_now(self, obj):
        return obj.is_active_on(timezone.localdate())

    def save_model(self, request, obj, form, change):
        from tasks.services import add_substitute_to_open_act_workflow_tasks

        if not change:
            obj.created_by = request.user
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if obj.is_active_on(timezone.localdate()):
                added = add_substitute_to_open_act_workflow_tasks(
                    obj.user, obj.role, actor=request.user
                )
                if added:
                    self.message_user(
                        request,
                        f'Сотрудник добавлен в открытые задачи этапов акта: {added}.',
                    )
