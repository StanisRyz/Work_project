from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import UserProfile
from acts.models import Act
from acts.services import ActWorkflowError, apply_ko_decision, apply_to_analysis, send_to_ko
from references.models import ActStatus, DefectType, Operation


class ActWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEFECT', name='Дефект')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)

    @classmethod
    def _create_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.save()
        return user

    def _create_act(self, status):
        return Act.objects.create(
            created_by=self.otk_user,
            party_number='P-001',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=status,
            description='Описание дефекта',
        )

    def test_otk_can_send_own_created_act_to_ko(self):
        act = self._create_act(self.status_created)

        send_to_ko(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')

    def test_ko_return_moves_act_back_to_created_otk(self):
        act = self._create_act(self.status_ko)

        apply_ko_decision(act, self.ko_user, Act.KoDecision.RETURN, 'На уточнение')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(act.ko_decision_by, self.ko_user)
        self.assertIsNotNone(act.ko_decision_at)

    def test_ko_allow_moves_act_to_to_analysis(self):
        act = self._create_act(self.status_ko)

        apply_ko_decision(act, self.ko_user, Act.KoDecision.ALLOW, 'Пропустить')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')

    def test_ko_reject_moves_act_to_to_analysis(self):
        act = self._create_act(self.status_ko)

        apply_ko_decision(act, self.ko_user, Act.KoDecision.REJECT, 'Не пропускать')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')

    def test_to_analysis_moves_act_to_actions_assigned(self):
        act = self._create_act(self.status_to)

        apply_to_analysis(act, self.to_user, 'Корневая причина', 'Мероприятия')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ACTIONS_ASSIGNED')
        self.assertEqual(act.to_analysis_by, self.to_user)
        self.assertIsNotNone(act.to_analysis_at)

    def test_wrong_roles_raise_workflow_error(self):
        ko_act = self._create_act(self.status_ko)
        to_act = self._create_act(self.status_to)
        created_act = self._create_act(self.status_created)

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(ko_act, self.otk_user, Act.KoDecision.ALLOW, '')
        with self.assertRaises(ActWorkflowError):
            apply_to_analysis(to_act, self.ko_user, 'Причина', 'Мероприятия')
        with self.assertRaises(ActWorkflowError):
            send_to_ko(created_act, self.to_user)
