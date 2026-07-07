from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from acts.models import Act
from references.models import ActStatus, DefectType, Operation


class ActViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEFECT', name='Дефект')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.other_otk_user = cls._create_user('other_otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)
        cls.manager_user = cls._create_user('manager', UserProfile.Role.MANAGER)
        cls.admin_user = cls._create_user('admin', UserProfile.Role.ADMIN)

    @classmethod
    def _create_user(cls, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.save()
        return user

    def _create_act(self, status, created_by=None, **kwargs):
        return Act.objects.create(
            created_by=created_by or self.otk_user,
            party_number=kwargs.get('party_number', 'P-001'),
            nomenclature=kwargs.get('nomenclature', 'Катушка'),
            operation=self.operation,
            defect_type=self.defect_type,
            status=status,
            description='Описание дефекта',
        )

    def test_otk_can_create_act_from_view(self):
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:create'),
            {
                'party_number': 'P-100',
                'nomenclature': 'Катушка А',
                'operation': self.operation.id,
                'defect_type': self.defect_type.id,
                'priority': '',
                'description': 'Описание дефекта',
                'due_date': '',
            },
        )

        self.assertEqual(response.status_code, 302)
        act = Act.objects.get(party_number='P-100')
        self.assertEqual(act.created_by, self.otk_user)
        self.assertEqual(act.status.code, 'CREATED_OTK')

    def test_direct_send_to_ko_uses_backend_permissions(self):
        act = self._create_act(self.status_created, created_by=self.otk_user)
        self.client.force_login(self.other_otk_user)

        response = self.client.post(reverse('acts:send_to_ko', args=[act.pk]))

        self.assertEqual(response.status_code, 404)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')

    def test_ko_can_apply_decision_from_view(self):
        act = self._create_act(self.status_ko)
        self.client.force_login(self.ko_user)

        response = self.client.post(
            reverse('acts:ko_decision', args=[act.pk]),
            {'ko_decision': Act.KoDecision.ALLOW, 'ko_comment': 'Пропустить'},
        )

        self.assertEqual(response.status_code, 302)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(act.ko_decision_by, self.ko_user)

    def test_to_can_apply_analysis_from_view(self):
        act = self._create_act(self.status_to)
        self.client.force_login(self.to_user)

        response = self.client.post(
            reverse('acts:to_analysis', args=[act.pk]),
            {'to_root_cause': 'Причина', 'to_action_summary': 'Мероприятия'},
        )

        self.assertEqual(response.status_code, 302)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ACTIONS_ASSIGNED')
        self.assertEqual(act.to_analysis_by, self.to_user)

    def test_wrong_role_direct_urls_do_not_bypass_checks(self):
        ko_act = self._create_act(self.status_ko)
        to_act = self._create_act(self.status_to)
        created_act = self._create_act(self.status_created)

        self.client.force_login(self.otk_user)
        response = self.client.post(
            reverse('acts:ko_decision', args=[ko_act.pk]),
            {'ko_decision': Act.KoDecision.ALLOW, 'ko_comment': 'Пропустить'},
        )
        self.assertEqual(response.status_code, 302)
        ko_act.refresh_from_db()
        self.assertEqual(ko_act.status.code, 'KO_REVIEW')

        self.client.force_login(self.ko_user)
        response = self.client.post(
            reverse('acts:to_analysis', args=[to_act.pk]),
            {'to_root_cause': 'Причина', 'to_action_summary': 'Мероприятия'},
        )
        self.assertEqual(response.status_code, 404)
        to_act.refresh_from_db()
        self.assertEqual(to_act.status.code, 'TO_ANALYSIS')

        self.client.force_login(self.to_user)
        response = self.client.post(reverse('acts:send_to_ko', args=[created_act.pk]))
        self.assertEqual(response.status_code, 404)
        created_act.refresh_from_db()
        self.assertEqual(created_act.status.code, 'CREATED_OTK')

    def test_manager_and_admin_can_see_all_acts(self):
        first_act = self._create_act(self.status_created, party_number='P-MANAGER')
        second_act = self._create_act(self.status_to, party_number='P-ADMIN')

        self.client.force_login(self.manager_user)
        response = self.client.get(reverse('acts:list'))
        self.assertContains(response, first_act.number)
        self.assertContains(response, second_act.number)

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('acts:list'))
        self.assertContains(response, first_act.number)
        self.assertContains(response, second_act.number)

    def test_detail_displays_only_available_actions(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertContains(response, reverse('acts:send_to_ko', args=[act.pk]))
        self.assertNotContains(response, reverse('acts:ko_decision', args=[act.pk]))
        self.assertNotContains(response, reverse('acts:to_analysis', args=[act.pk]))
