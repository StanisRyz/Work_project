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


# The five roles added for the remaining departments. They carry no rights of
# their own, so what is worth pinning is exactly that: they exist, they are
# assignable, and a user holding one is an ordinary authenticated user.
DEPARTMENT_ROLES = (
    (UserProfile.Role.OPR, 'opr', 'Отдел продаж'),
    (UserProfile.Role.OZK, 'ozk', 'Отдел закупок'),
    (UserProfile.Role.LAB, 'lab', 'Лаборатория'),
    (UserProfile.Role.SKL, 'skl', 'Склад'),
    (UserProfile.Role.FEO, 'feo', 'ФЭО'),
)


class DepartmentRoleTests(TestCase):
    """The three things the new roles must do, and nothing more.

    They add no permission, so there is nothing else to test: reading is the
    ordinary authenticated read, and completing a task is `TaskAssignee`'s
    answer, which every existing task test already covers.
    """

    def test_an_administrator_can_assign_every_new_role(self):
        """Assignable through the admin form, and captioned in Russian.

        The form is the one `UserProfileAdmin` builds, so this is the widget an
        administrator really uses: a value the choices did not carry would be
        rejected by it rather than silently stored.
        """
        from django.contrib import admin
        from django.test import RequestFactory

        superuser = User.objects.create_superuser(
            username='root', password='demo12345', email='root@example.com',
        )
        request = RequestFactory().get('/admin/')
        request.user = superuser
        form_class = admin.site._registry[UserProfile].get_form(request, obj=None)
        offered = dict(form_class.base_fields['role'].choices)

        department = Department.objects.create(code='SALES', name='Отдел продаж')
        for role, value, label in DEPARTMENT_ROLES:
            with self.subTest(role=value):
                self.assertEqual(role.value, value)
                self.assertEqual(role.label, label)
                self.assertIn(role.value, offered)
                self.assertEqual(str(offered[role.value]), label)

                user = User.objects.create_user(
                    username=f'{value}_user', password='demo12345',
                )
                form = form_class(
                    data={
                        'user': user.pk,
                        'department': department.pk,
                        'role': role.value,
                        'position': '',
                        'internal_phone': '',
                        'is_active': 'on',
                    },
                    instance=user.userprofile,
                )
                self.assertTrue(form.is_valid(), form.errors)
                form.save()
                user.refresh_from_db()
                self.assertEqual(user.userprofile.role, role.value)
                # What the topbar and the sidebar show.
                self.assertEqual(user.userprofile.role_label, label)

    def test_a_user_with_a_new_role_logs_in_reads_and_completes_own_task(self):
        """Login, the ordinary read scope, and finishing an assigned task.

        Nothing here is role-specific by design: the same three answers an ОТК
        or a МАС employee gets. The task is completed because its assignee is
        this user — which is why no permission had to be added for the role.
        """
        from bugs.services import report_bug
        from tasks.services import TaskWorkflowError, complete_task

        department = Department.objects.create(code='WH', name='Склад')
        user = User.objects.create_user(username='skl_user', password='demo12345')
        user.userprofile.role = UserProfile.Role.SKL
        user.userprofile.department = department
        user.userprofile.is_bug_responsible = True
        user.userprofile.save()
        reporter = User.objects.create_user(username='reporter', password='demo12345')

        # 1. Logs in.
        self.assertTrue(self.client.login(username='skl_user', password='demo12345'))

        # 2. Reads every section open to an authenticated user.
        for route in ('acts:list', 'tasks:list', 'protocols:list', 'smk:list'):
            with self.subTest(route=route):
                self.assertEqual(self.client.get(reverse(route)).status_code, 200)

        # 3. Completes a task assigned to them. A bug-report task is used
        # because it is the one work item needing no quality document behind
        # it — what is checked is the assignee rule, not the source.
        own = report_bug(reporter=reporter, message='Не работает поиск.').task
        self.assertIn(
            own.pk,
            [row['task'].pk for row in self.client.get(reverse('tasks:list')).context['rows']],
        )
        response = self.client.post(
            reverse('tasks:complete', args=[own.pk]),
            {'execution_comment': 'Проверено, поиск работает.'},
        )
        self.assertEqual(response.status_code, 302)
        own.refresh_from_db()
        self.assertEqual(own.status.code, 'COMPLETED')
        self.assertEqual(own.completed_by, user)

        # And somebody else's task stays somebody else's — the role grants no
        # shortcut into it.
        user.userprofile.is_bug_responsible = False
        user.userprofile.save(update_fields=['is_bug_responsible'])
        stranger = User.objects.create_user(username='stranger', password='demo12345')
        stranger.userprofile.is_bug_responsible = True
        stranger.userprofile.save(update_fields=['is_bug_responsible'])
        foreign = report_bug(reporter=reporter, message='Другая ошибка.').task
        self.assertEqual(
            self.client.post(
                reverse('tasks:complete', args=[foreign.pk]),
                {'execution_comment': 'Не моё.'},
            ).status_code,
            404,
        )
        # Refused by the service too, not only by the view that calls it.
        with self.assertRaises(TaskWorkflowError):
            complete_task(foreign, user, 'Не моё.')


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
    """The dashboard at `/` is where every authenticated user lands, including administrators."""

    def _create_user(self, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.save()
        return user

    def test_login_sends_a_normal_user_to_the_dashboard(self):
        self._create_user('otk_landing', UserProfile.Role.OTK)

        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'otk_landing', 'password': 'demo12345'},
        )

        self.assertRedirects(response, reverse('dashboard:home'))
        # `/` is the dashboard itself now, not a redirect to a module.
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_an_administrator_also_lands_on_the_dashboard(self):
        self._create_user('admin_landing', UserProfile.Role.ADMIN)

        response = self.client.post(
            reverse('accounts:login'),
            {'username': 'admin_landing', 'password': 'demo12345'},
        )

        self.assertRedirects(response, reverse('dashboard:home'))

    def test_login_still_honours_next_for_a_protected_page(self):
        self._create_user('otk_next', UserProfile.Role.OTK)
        target = reverse('tasks:list')

        response = self.client.post(
            f"{reverse('accounts:login')}?next={target}",
            {'username': 'otk_next', 'password': 'demo12345'},
        )

        self.assertRedirects(response, target)


class PasswordChangeTests(TestCase):
    """«Сменить пароль» in the profile menu, end to end.

    Django's own view, form and hashing do the work, so what is worth pinning
    is the flow around them: the menu offers it, the current password must be
    right, a successful change logs the user in with the new one *without*
    ending the session, and the target it returns to is validated.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='changer', password='demo12345')
        profile = self.user.userprofile
        profile.role = UserProfile.Role.OTK
        profile.save()
        self.client.force_login(self.user)
        self.url = reverse('accounts:password_change')

    def test_the_profile_menu_offers_password_change_beside_logout(self):
        page = self.client.get(reverse('dashboard:home'))
        self.assertContains(page, 'Сменить пароль')
        self.assertContains(page, self.url)
        self.assertContains(page, 'data-password-modal')

    def test_a_user_changes_their_password_and_stays_signed_in(self):
        target = reverse('tasks:list')
        response = self.client.post(
            self.url,
            {
                'old_password': 'demo12345',
                'new_password1': 'novyparol987',
                'new_password2': 'novyparol987',
                'next': target,
            },
        )

        self.assertRedirects(response, target)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('novyparol987'))
        # The session survives the change: the very next request is still
        # authenticated, with no trip through the login page.
        landing = self.client.get(target)
        self.assertEqual(landing.status_code, 200)
        self.assertEqual(landing.context['user'], self.user)
        # And the old password no longer works anywhere.
        self.client.logout()
        self.assertFalse(
            self.client.login(username='changer', password='demo12345')
        )
        self.assertTrue(
            self.client.login(username='changer', password='novyparol987')
        )

    def test_a_wrong_current_password_or_a_mismatch_changes_nothing(self):
        wrong = self.client.post(
            self.url,
            {
                'old_password': 'ne-tot-parol',
                'new_password1': 'novyparol987',
                'new_password2': 'novyparol987',
            },
        )
        self.assertEqual(wrong.status_code, 200)
        self.assertTrue(wrong.context['form'].errors)

        mismatch = self.client.post(
            self.url,
            {
                'old_password': 'demo12345',
                'new_password1': 'novyparol987',
                'new_password2': 'drugoyparol987',
            },
        )
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn('new_password2', mismatch.context['form'].errors)

        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('demo12345'))

    def test_an_off_host_return_target_is_ignored(self):
        response = self.client.post(
            self.url,
            {
                'old_password': 'demo12345',
                'new_password1': 'novyparol987',
                'new_password2': 'novyparol987',
                'next': 'https://example.invalid/steal',
            },
        )
        self.assertRedirects(response, reverse('dashboard:home'))

    def test_an_anonymous_visitor_is_sent_to_the_login_page(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('accounts:login'), response['Location'])
