"""The destructive demo reset is a feature flag, not a permission on a name.

Two independent gates: `ENABLE_DEMO_RESET` decides whether the route exists in
this deployment at all, and the administrator role decides who may use it where
it does. Production forces the flag off, so the URL is never registered there.
"""

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import NoReverseMatch, reverse

from accounts.models import UserProfile
from acts.models import Act
from acts.permissions import can_clear_all_acts
from ecosystem.testing import demo_reset_enabled
from references.models import ActStatus, DefectType, Operation


class DemoResetUrlRegistrationTests(TestCase):
    def test_the_url_does_not_exist_by_default(self):
        with self.assertRaises(NoReverseMatch):
            reverse('acts:clear_all')

    def test_the_url_exists_only_while_the_flag_is_on(self):
        with demo_reset_enabled():
            self.assertTrue(reverse('acts:clear_all').endswith('/clear-all/'))

        with self.assertRaises(NoReverseMatch):
            reverse('acts:clear_all')

    def test_a_direct_request_is_a_plain_404_when_the_flag_is_off(self):
        # The path itself, without reverse(): a deployment with the flag off
        # must answer as if the feature never existed.
        response = self.client.post('/quality/acts/clear-all/')

        self.assertEqual(response.status_code, 404)


class DemoResetPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.administrator = cls._user('deploy_admin', UserProfile.Role.ADMIN)
        cls.otk = cls._user('deploy_otk', UserProfile.Role.OTK)
        cls.manager = cls._user('deploy_manager', UserProfile.Role.MANAGER)
        cls.operation = Operation.objects.create(code='DR_OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DR_DEF', name='Дефект')
        cls.status = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )[0]

    @classmethod
    def _user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save()
        return user

    def _act(self):
        return Act.objects.create(
            created_by=self.otk,
            nomenclature='Изделие',
            status=self.status,
        )

    def test_the_permission_is_false_while_the_flag_is_off(self):
        # Even for an administrator: the flag is the outer gate.
        self.assertFalse(can_clear_all_acts(self.administrator))

    def test_the_permission_no_longer_depends_on_a_username(self):
        renamed = self._user('admin_user', UserProfile.Role.OTK)

        with demo_reset_enabled():
            # The old rule granted this by name alone; the role must decide.
            self.assertFalse(can_clear_all_acts(renamed))
            self.assertTrue(can_clear_all_acts(self.administrator))

    def test_an_ordinary_user_cannot_clear_anything(self):
        self._act()

        with demo_reset_enabled():
            self.client.force_login(self.otk)
            response = self.client.post(reverse('acts:clear_all'))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Act.objects.count(), 1)

    def test_a_manager_without_the_administrator_role_cannot_clear_anything(self):
        self._act()

        with demo_reset_enabled():
            self.client.force_login(self.manager)
            response = self.client.post(reverse('acts:clear_all'))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(Act.objects.count(), 1)

    def test_a_get_is_never_destructive(self):
        self._act()

        with demo_reset_enabled():
            self.client.force_login(self.administrator)
            response = self.client.get(reverse('acts:clear_all'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Act.objects.count(), 1)

    def test_csrf_protection_is_still_enforced(self):
        self._act()

        with demo_reset_enabled():
            enforcing = self.client_class(enforce_csrf_checks=True)
            enforcing.force_login(self.administrator)
            response = enforcing.post(reverse('acts:clear_all'))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Act.objects.count(), 1)

    def test_an_administrator_can_clear_acts_while_the_flag_is_on(self):
        self._act()

        with demo_reset_enabled():
            self.client.force_login(self.administrator)
            response = self.client.post(reverse('acts:clear_all'))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Act.objects.count(), 0)


class DemoResetProductionTests(TestCase):
    def test_production_forces_the_flag_off_even_when_requested(self):
        from ecosystem import checks

        with override_settings(IS_PRODUCTION=True, DEMO_RESET_REQUESTED=True):
            reported = {message.id for message in checks._check_demo_reset()}

        self.assertIn('ecosystem.E013', reported)
