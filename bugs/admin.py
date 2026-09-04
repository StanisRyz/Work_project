"""The bug reports, read in Django Admin.

Deliberately read-only: a report is a statement somebody made, and editing it
would rewrite what they said. Who *receives* one is not set here either — it is
`accounts.UserProfile.is_bug_responsible`, a column of `UserProfileAdmin`.
"""

from django.contrib import admin

from .models import BugReport


@admin.register(BugReport)
class BugReportAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'reporter', 'short_message', 'page_url', 'created_at')
    list_filter = ('created_at', 'reporter')
    search_fields = ('message', 'page_url', 'reporter__username', 'reporter__last_name')
    date_hierarchy = 'created_at'
    readonly_fields = ('reporter', 'message', 'page_url', 'created_at')

    @admin.display(description='Описание')
    def short_message(self, obj):
        return obj.message if len(obj.message) <= 80 else f'{obj.message[:80]}…'

    def has_add_permission(self, request):
        """Reports are filed from the application, never typed in here."""
        return False

    def has_change_permission(self, request, obj=None):
        return False
