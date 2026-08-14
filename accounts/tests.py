from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, UserProfile


class DemoAccountCommandTests(TestCase):
    def test_demo_accounts_are_blocked_outside_development_before_database_changes(self):
        for app_env in ('test', 'production'):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, 'available only'):
                    call_command('seed_demo_accounts', confirm_demo=True)

        self.assertFalse(Department.objects.exists())


class LandingRedirectTests(TestCase):
    """`/acts/` is the working page for every authenticated user, including administrators."""

    def _create_user(self, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.save()
        return user

    def test_login_and_root_send_a_normal_user_to_the_acts_registry(self):
        self._create_user('otk_landing', UserProfile.Role.OTK)

        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'otk_landing', 'password': 'demo12345'},
        )

        self.assertRedirects(response, reverse('acts:list'))
        self.assertRedirects(self.client.get('/'), reverse('acts:list'))

    def test_an_administrator_also_lands_on_the_acts_registry(self):
        self._create_user('admin_landing', UserProfile.Role.ADMIN)

        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'admin_landing', 'password': 'demo12345'},
        )

        self.assertRedirects(response, reverse('acts:list'))

    def test_login_still_honours_next_for_a_protected_page(self):
        self._create_user('otk_next', UserProfile.Role.OTK)
        target = reverse('tasks:list')

        response = self.client.post(
            f"{reverse('accounts:login')}?next={target}",
            {'username': 'otk_next', 'password': 'demo12345'},
        )

        self.assertRedirects(response, target)
