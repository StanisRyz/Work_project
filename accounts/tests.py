from importlib import import_module
from types import SimpleNamespace

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import Department, UserProfile


class MasRoleAndDepartmentTests(TestCase):
    def test_mas_is_a_first_class_role_and_the_department_is_migration_seeded(self):
        self.assertEqual(UserProfile.Role.MAS.value, 'mas')
        self.assertEqual(UserProfile.Role.MAS.label, 'Мастер производства')

        department = Department.objects.get(code='MAS')
        self.assertEqual(department.name, 'Мастера производства')
        self.assertTrue(department.is_active)

    def test_mas_department_seed_is_idempotent_and_does_not_reassign_users(self):
        user = User.objects.create_user(username='existing_otk', password='demo12345')
        original_department = Department.objects.create(code='OTK', name='ОТК')
        UserProfile.objects.filter(user=user).update(
            role=UserProfile.Role.OTK,
            department=original_department,
        )
        Department.objects.filter(code='MAS').delete()
        existing_mas = Department.objects.create(
            code='LEGACY_MAS',
            name='Мастера производства',
            is_active=False,
        )

        migration = import_module('accounts.migrations.0005_mas_department')
        schema_editor = SimpleNamespace(connection=connection)
        migration.ensure_mas_department(apps, schema_editor)
        migration.ensure_mas_department(apps, schema_editor)

        seeded_mas = Department.objects.get(code='MAS')
        self.assertEqual(seeded_mas.pk, existing_mas.pk)
        self.assertTrue(seeded_mas.is_active)
        self.assertEqual(Department.objects.filter(name='Мастера производства').count(), 1)
        user.refresh_from_db()
        self.assertEqual(user.userprofile.role, UserProfile.Role.OTK)
        self.assertEqual(user.userprofile.department, original_department)

    def test_mas_sees_normal_navigation_but_has_no_django_admin_access(self):
        user = User.objects.create_user(username='mas_navigation', password='demo12345')
        UserProfile.objects.filter(user=user).update(role=UserProfile.Role.MAS)
        user.refresh_from_db()
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

        self.client.force_login(user)
        page = self.client.get(reverse('acts:list'))
        self.assertEqual(page.status_code, 200)
        for label in (
            'Акты',
            'Задачи',
            'Протоколы',
            'Калькулятор времени навивки',
            'Калькулятор рубки пластин',
        ):
            self.assertContains(page, label)
        self.assertNotContains(page, 'Создать акт')
        self.assertRedirects(
            self.client.get(reverse('admin:index')),
            f"{reverse('admin:login')}?next={reverse('admin:index')}",
        )


class DemoAccountCommandTests(TestCase):
    def test_demo_accounts_are_blocked_outside_development_before_database_changes(self):
        for app_env in ('test', 'production'):
            with self.subTest(app_env=app_env), override_settings(APP_ENV=app_env):
                with self.assertRaisesMessage(CommandError, 'available only'):
                    call_command('seed_demo_accounts', confirm_demo=True)

        # The command made no changes at all. Only the departments it would
        # have seeded are checked: reference departments created by data
        # migrations — PDO and MAS — legitimately exist.
        self.assertFalse(
            Department.objects.filter(code__in=['OTK', 'KO', 'TO', 'MANAGEMENT']).exists(),
        )
        self.assertFalse(User.objects.exists())


class LandingRedirectTests(TestCase):
    """`/quality/acts/` is the working page for every authenticated user, including administrators."""

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
