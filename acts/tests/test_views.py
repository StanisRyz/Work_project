from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActDefect
from references.models import ActStatus, DefectType, Operation, Priority


class ActViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.operation = Operation.objects.create(code='OPERATIONAL_CONTROL', name='Операционный контроль')
        cls.defect_type = DefectType.objects.create(code='SIZE_NONCONFORMITY', name='Несоответствие размеров')
        cls.priority = Priority.objects.create(code='HIGH', name='Высокий')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.other_otk_user = cls._create_user('other_otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)
        cls.manager_user = cls._create_user('manager', UserProfile.Role.MANAGER)
        cls.admin_user = cls._create_user('admin', UserProfile.Role.ADMIN)
        cls.no_profile_user = User.objects.create_user(username='no_profile', password='demo12345')
        cls.no_profile_user.userprofile.delete()
        cls.no_profile_user._state.fields_cache.pop('userprofile', None)

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
            priority=kwargs.get('priority'),
            status=status,
            description='Описание дефекта',
            due_date=kwargs.get('due_date'),
        )

    def test_otk_can_create_act_from_view(self):
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:create'),
            {
                'customer': 'Заказчик',
                'order_number': '100-1',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-100',
                'defects-TOTAL_FORMS': '1',
                'defects-INITIAL_FORMS': '0',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                'defects-0-defect_type': self.defect_type.id,
                'defects-0-operation': self.operation.id,
                'defects-0-mp_type': 'OL',
                'defects-0-znp_number': '200-1',
                'defects-0-party_number': '100-100',
                'defects-0-checked_quantity': '100',
                'defects-0-nonconforming_quantity': '4',
                'defects-0-description': 'Описание дефекта',
                'defects-0-detected_at': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        act = Act.objects.get(party_number='100-100')
        self.assertEqual(act.created_by, self.otk_user)
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(ActDefect.objects.filter(act=act).count(), 1)

    def test_create_rejects_nonconforming_quantity_above_checked_quantity(self):
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:create'),
            {
                'customer': 'Заказчик',
                'order_number': '100-2',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-101',
                'defects-TOTAL_FORMS': '1',
                'defects-INITIAL_FORMS': '0',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                'defects-0-defect_type': self.defect_type.id,
                'defects-0-operation': self.operation.id,
                'defects-0-mp_type': 'OL',
                'defects-0-znp_number': '200-2',
                'defects-0-party_number': '100-101',
                'defects-0-checked_quantity': '4',
                'defects-0-nonconforming_quantity': '5',
                'defects-0-description': 'Описание дефекта',
                'defects-0-detected_at': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'не может превышать')

    def test_otk_list_shows_only_own_created_otk_acts(self):
        visible = self._create_act(self.status_created, party_number='P-OTK')
        hidden_other = self._create_act(self.status_created, created_by=self.other_otk_user, party_number='P-OTHER')
        hidden_stage = self._create_act(self.status_ko, party_number='P-KO')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        self.assertContains(response, visible.number)
        self.assertNotContains(response, hidden_other.number)
        self.assertNotContains(response, hidden_stage.number)

    def test_only_dedicated_admin_user_can_clear_all_acts(self):
        self._create_act(self.status_created, party_number='P-CLEAR-1')
        self._create_act(self.status_ko, party_number='P-CLEAR-2')
        dedicated_admin = self._create_user('admin_user', UserProfile.Role.ADMIN)

        self.client.force_login(self.admin_user)
        self.assertEqual(self.client.post(reverse('acts:clear_all')).status_code, 404)

        self.client.force_login(dedicated_admin)
        response = self.client.post(reverse('acts:clear_all'))

        self.assertRedirects(response, reverse('acts:list'))
        self.assertEqual(Act.objects.count(), 0)

    def test_direct_send_to_ko_uses_backend_permissions(self):
        act = self._create_act(self.status_created, created_by=self.otk_user)
        self.client.force_login(self.other_otk_user)

        response = self.client.post(reverse('acts:send_to_ko', args=[act.pk]))

        self.assertEqual(response.status_code, 404)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')

    def test_otk_does_not_see_own_act_after_sending_to_ko(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.otk_user)

        response = self.client.post(reverse('acts:send_to_ko', args=[act.pk]))

        self.assertRedirects(response, reverse('acts:list'))
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(self.client.get(reverse('acts:detail', args=[act.pk])).status_code, 404)

    def test_ko_sees_only_ko_review_acts(self):
        visible = self._create_act(self.status_ko, party_number='P-KO')
        hidden_created = self._create_act(self.status_created, party_number='P-OTK')
        hidden_to = self._create_act(self.status_to, party_number='P-TO')
        self.client.force_login(self.ko_user)

        response = self.client.get(reverse('acts:list'))

        self.assertContains(response, visible.number)
        self.assertNotContains(response, hidden_created.number)
        self.assertNotContains(response, hidden_to.number)

    def test_ko_does_not_see_act_after_new_decision(self):
        for decision in Act.KoDecision.new_values():
            act = self._create_act(self.status_ko)
            self.client.force_login(self.ko_user)

            response = self.client.post(
                reverse('acts:ko_decision', args=[act.pk]),
                {'ko_decision': decision, 'ko_comment': 'Решение'},
            )

            self.assertRedirects(response, reverse('acts:list'))
            act.refresh_from_db()
            self.assertEqual(act.status.code, 'TO_ANALYSIS')
            self.assertEqual(self.client.get(reverse('acts:detail', args=[act.pk])).status_code, 404)

    def test_to_sees_only_to_analysis_acts(self):
        visible = self._create_act(self.status_to, party_number='P-TO')
        hidden_ko = self._create_act(self.status_ko, party_number='P-KO')
        hidden_actions = self._create_act(self.status_actions, party_number='P-ACTIONS')
        self.client.force_login(self.to_user)

        response = self.client.get(reverse('acts:list'))

        self.assertContains(response, visible.number)
        self.assertNotContains(response, hidden_ko.number)
        self.assertNotContains(response, hidden_actions.number)

    def test_to_does_not_see_act_after_analysis(self):
        act = self._create_act(self.status_to)
        self.client.force_login(self.to_user)

        response = self.client.post(
            reverse('acts:to_analysis', args=[act.pk]),
            {'to_root_cause': 'Причина', 'to_action_summary': 'Мероприятия'},
        )

        self.assertRedirects(response, reverse('acts:detail', args=[act.pk]))
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ACTIONS_ASSIGNED')
        self.assertEqual(self.client.get(reverse('acts:detail', args=[act.pk])).status_code, 200)

    def test_wrong_role_direct_urls_do_not_bypass_checks(self):
        ko_act = self._create_act(self.status_ko)
        to_act = self._create_act(self.status_to)
        created_act = self._create_act(self.status_created)

        self.client.force_login(self.otk_user)
        response = self.client.post(
            reverse('acts:ko_decision', args=[ko_act.pk]),
            {'ko_decision': Act.KoDecision.ALLOW_NO_REWORK, 'ko_comment': 'Пропустить'},
        )
        self.assertEqual(response.status_code, 404)
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
        third_act = self._create_act(self.status_actions, party_number='P-ACTIONS')

        self.client.force_login(self.manager_user)
        response = self.client.get(reverse('acts:list'))
        self.assertContains(response, first_act.number)
        self.assertContains(response, second_act.number)
        self.assertContains(response, third_act.number)
        self.assertNotContains(response, 'Режим администратора: полный доступ к актам.')

        self.client.force_login(self.admin_user)
        response = self.client.get(reverse('acts:list'))
        self.assertContains(response, first_act.number)
        self.assertContains(response, second_act.number)
        self.assertContains(response, third_act.number)
        self.assertContains(response, 'Режим администратора: полный доступ к актам.')

    def test_user_without_profile_sees_no_acts(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.no_profile_user)

        response = self.client.get(reverse('acts:list'))

        self.assertEqual(response.context['kpis']['total'], 0)
        self.assertNotContains(response, act.number)

    def test_direct_detail_url_for_hidden_act_is_not_accessible(self):
        hidden_act = self._create_act(self.status_ko)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[hidden_act.pk]))

        self.assertEqual(response.status_code, 404)

    def test_list_kpi_counters_use_only_visible_acts(self):
        today = timezone.localdate()
        self._create_act(self.status_created, due_date=today - timedelta(days=1), priority=self.priority)
        self._create_act(self.status_ko, due_date=today - timedelta(days=1))
        self._create_act(self.status_to, due_date=today - timedelta(days=1))
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        self.assertEqual(response.context['kpis']['total'], 1)
        self.assertEqual(response.context['kpis']['overdue'], 1)
        self.assertEqual(response.context['kpis']['created_otk'], 1)
        self.assertEqual(response.context['kpis']['ko_review'], 0)
        self.assertEqual(response.context['kpis']['to_analysis'], 0)

    def test_detail_displays_only_available_actions(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertContains(response, reverse('acts:send_to_ko', args=[act.pk]))
        self.assertNotContains(response, reverse('acts:ko_decision', args=[act.pk]))
        self.assertNotContains(response, reverse('acts:to_analysis', args=[act.pk]))
