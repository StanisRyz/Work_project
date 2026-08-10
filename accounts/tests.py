from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import Department


class DemoAccountCommandTests(TestCase):
    def test_demo_accounts_are_blocked_outside_development_before_database_changes(self):
        for app_env in ('test', 'production'):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, 'available only'):
                    call_command('seed_demo_accounts', confirm_demo=True)

        self.assertFalse(Department.objects.exists())
