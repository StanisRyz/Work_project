from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import UserProfile
from acts.models import Act
from acts.permissions import (
    can_apply_ko_decision,
    can_apply_to_analysis,
    can_create_act,
    can_send_to_ko,
    can_view_act,
    get_user_profile,
    get_user_role,
    get_visible_acts_queryset,
)
from references.models import ActStatus, DefectType, Operation


class ActPermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.status_closed = ActStatus.objects.create(code='CLOSED', name='Закрыт')
        cls.status_cancelled = ActStatus.objects.create(code='CANCELLED', name='Отменен')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEFECT', name='Дефект')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.other_otk_user = cls._create_user('other_otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)
        cls.manager_user = cls._create_user('manager', UserProfile.Role.MANAGER)
        cls.admin_user = cls._create_user('admin', UserProfile.Role.ADMIN)
        cls.no_profile_user = User.objects.create_user(username='no_profile', password='demo12345')
        cls.no_profile_user.userprofile.delete()
        cls.no_profile_user._state.fields_cache.pop('userprofile', None)

        cls.created_act = cls._create_act(cls.status_created)
        cls.other_otk_created_act = cls._create_act(cls.status_created, created_by=cls.other_otk_user)
        cls.ko_act = cls._create_act(cls.status_ko)
        cls.to_act = cls._create_act(cls.status_to)
        cls.actions_act = cls._create_act(cls.status_actions)
        cls.closed_act = cls._create_act(cls.status_closed)
        cls.cancelled_act = cls._create_act(cls.status_cancelled)

    @classmethod
    def _create_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.save()
        return user

    @classmethod
    def _create_act(cls, status, created_by=None):
        return Act.objects.create(
            created_by=created_by or cls.otk_user,
            party_number='P-001',
            nomenclature='Катушка',
            operation=cls.operation,
            defect_type=cls.defect_type,
            status=status,
            description='Описание дефекта',
        )

    def test_otk_can_create_act(self):
        self.assertTrue(can_create_act(self.otk_user))

    def test_otk_can_send_own_created_act_to_ko(self):
        self.assertTrue(can_send_to_ko(self.created_act, self.otk_user))
        self.assertFalse(can_send_to_ko(self.created_act, self.other_otk_user))

    def test_wrong_roles_cannot_apply_workflow_actions(self):
        self.assertFalse(can_apply_ko_decision(self.ko_act, self.otk_user))
        self.assertFalse(can_apply_to_analysis(self.to_act, self.ko_user))
        self.assertFalse(can_send_to_ko(self.created_act, self.to_user))

    def test_manager_and_admin_can_process_current_status_actions(self):
        self.assertTrue(can_send_to_ko(self.created_act, self.manager_user))
        self.assertTrue(can_apply_ko_decision(self.ko_act, self.admin_user))
        self.assertTrue(can_apply_to_analysis(self.to_act, self.manager_user))

    def test_missing_user_profile_does_not_crash_permission_helpers(self):
        self.assertIsNone(get_user_profile(self.no_profile_user))
        self.assertEqual(get_user_role(self.no_profile_user), '')
        self.assertFalse(can_create_act(self.no_profile_user))
        self.assertFalse(can_send_to_ko(self.created_act, self.no_profile_user))

    def test_otk_sees_only_own_created_otk_acts(self):
        queryset = get_visible_acts_queryset(self.otk_user)

        self.assertIn(self.created_act, queryset)
        self.assertNotIn(self.other_otk_created_act, queryset)
        self.assertNotIn(self.ko_act, queryset)
        self.assertFalse(can_view_act(self.ko_act, self.otk_user))

    def test_ko_sees_only_ko_review_acts(self):
        queryset = get_visible_acts_queryset(self.ko_user)

        self.assertIn(self.ko_act, queryset)
        self.assertNotIn(self.created_act, queryset)
        self.assertNotIn(self.to_act, queryset)

    def test_to_sees_only_to_analysis_acts(self):
        queryset = get_visible_acts_queryset(self.to_user)

        self.assertIn(self.to_act, queryset)
        self.assertNotIn(self.ko_act, queryset)
        self.assertNotIn(self.actions_act, queryset)

    def test_actions_closed_and_cancelled_are_manager_admin_only(self):
        for act in (self.actions_act, self.closed_act, self.cancelled_act):
            self.assertFalse(can_view_act(act, self.otk_user))
            self.assertFalse(can_view_act(act, self.ko_user))
            self.assertFalse(can_view_act(act, self.to_user))
            self.assertTrue(can_view_act(act, self.manager_user))
            self.assertTrue(can_view_act(act, self.admin_user))

    def test_manager_admin_and_user_without_profile_visibility(self):
        self.assertEqual(get_visible_acts_queryset(self.manager_user).count(), Act.objects.count())
        self.assertEqual(get_visible_acts_queryset(self.admin_user).count(), Act.objects.count())
        self.assertEqual(get_visible_acts_queryset(self.no_profile_user).count(), 0)
