from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActComment, ActCorrectiveAction, ActCorrectiveActionAssignee, ActDefect, ActHistoryEvent, ActRootAnalysis
from references.models import ActStatus, DefectType, Operation, Priority, TaskStatus
from tasks.models import Task, TaskAssignee


class ActViewTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.status_otk_review = ActStatus.objects.get(code='OTK_REVIEW')
        cls.status_archived = ActStatus.objects.get(code='ARCHIVED')
        cls.operation = Operation.objects.create(code='OPERATIONAL_CONTROL', name='Операционный контроль')
        cls.defect_type = DefectType.objects.create(code='SIZE_NONCONFORMITY', name='Несоответствие размеров')
        cls.priority = Priority.objects.create(code='HIGH', name='Высокий')
        cls.department = Department.objects.create(code='TO', name='Технологический отдел')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.other_otk_user = cls._create_user('other_otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)
        cls.to_user.userprofile.department = cls.department
        cls.to_user.userprofile.save()
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
            act_type=kwargs.get('act_type', Act.Type.OPERATIONAL_CONTROL),
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
                'defects-0-workshop': ActDefect.Workshop.MP_SHOP,
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
        self.assertEqual(act.defects.get().workshop, ActDefect.Workshop.MP_SHOP)

    def test_act_form_uses_compact_defect_groups(self):
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:create'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Создание акта')
        self.assertContains(response, 'Операционный контроль')
        self.assertContains(response, 'class="act-form-section act-defect-section"', html=False)
        self.assertContains(response, 'class="act-form-page__back"', html=False)
        self.assertContains(response, 'data-defect-count')
        for group in ('Партия', 'Контроль', 'Результат контроля'):
            self.assertContains(response, f'<legend>{group}</legend>', html=False)
        self.assertContains(response, 'Добавить ещё дефект')

    def test_create_form_hides_defect_fields_until_workshop_chosen(self):
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:create'))
        content = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Цех/поставщик')
        self.assertContains(response, 'Цех МП')
        self.assertContains(response, 'Цех трансформаторов')
        self.assertContains(response, 'data-defect-collapsible', html=False)

        workshop_select_index = content.index('name="defects-0-workshop"')
        empty_option_index = content.index('<option value="" selected>', workshop_select_index)
        mp_option_index = content.index('value="MP_SHOP"', workshop_select_index)
        self.assertLess(empty_option_index, mp_option_index)

        # The workshop select itself must not be marked collapsible.
        workshop_field_index = content.index('name="defects-0-workshop"')
        collapsible_before_workshop = content.rfind('data-defect-collapsible', 0, workshop_field_index)
        znp_field_index = content.index('name="defects-0-znp_number"')
        collapsible_before_znp = content.rfind('data-defect-collapsible', 0, znp_field_index)
        self.assertGreater(collapsible_before_znp, workshop_field_index)
        self.assertEqual(collapsible_before_workshop, -1)

    def test_history_feed_renders_return_and_comment_as_separate_events(self):
        act = self._create_act(self.status_created)
        ActHistoryEvent.objects.create(
            act=act,
            user=self.ko_user,
            event_type=ActHistoryEvent.EventType.RETURNED_TO_OTK,
            message='Акт возвращён в ОТК на доработку.',
            from_status=self.status_ko,
            to_status=self.status_created,
        )
        ActHistoryEvent.objects.create(
            act=act,
            user=self.ko_user,
            event_type=ActHistoryEvent.EventType.COMMENT_ADDED,
            message='Уточнить решение КО.',
        )
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]), {'tab': 'history'})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'history-feed__filter')
        self.assertContains(response, 'history-event--returned_to_otk')
        self.assertContains(response, 'history-event--comment_added')
        self.assertContains(response, 'На рассмотрении КО → Создан ОТК')
        self.assertContains(response, 'Комментарий добавлен')

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
                'defects-0-workshop': ActDefect.Workshop.MP_SHOP,
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

    def _defect_post_fields(self, index, workshop, **overrides):
        fields = {
            f'defects-{index}-workshop': workshop,
            f'defects-{index}-defect_type': self.defect_type.id,
            f'defects-{index}-operation': self.operation.id,
            f'defects-{index}-mp_type': 'OL',
            f'defects-{index}-znp_number': f'20{index}-1',
            f'defects-{index}-party_number': f'10{index}-100',
            f'defects-{index}-checked_quantity': '10',
            f'defects-{index}-nonconforming_quantity': '1',
            f'defects-{index}-description': 'Описание дефекта',
            f'defects-{index}-detected_at': timezone.localdate().isoformat(),
        }
        for key, value in overrides.items():
            fields[f'defects-{index}-{key}'] = value
        return fields

    def test_create_act_succeeds_for_each_workshop_choice(self):
        self.client.force_login(self.otk_user)

        for choice_index, workshop in enumerate(ActDefect.Workshop.values):
            order_number = f'300-{choice_index}'
            response = self.client.post(
                reverse('acts:create'),
                {
                    'customer': 'Заказчик',
                    'order_number': order_number,
                    'nomenclature': 'Катушка-А',
                    'kd_designation': 'КД-200',
                    'defects-TOTAL_FORMS': '1',
                    'defects-INITIAL_FORMS': '0',
                    'defects-MIN_NUM_FORMS': '1',
                    'defects-MAX_NUM_FORMS': '1000',
                    **self._defect_post_fields(0, workshop),
                },
            )

            self.assertEqual(response.status_code, 302, workshop)
            act = Act.objects.get(order_number=order_number)
            self.assertEqual(act.defects.get().workshop, workshop)

    def test_create_rejects_act_without_workshop_selection(self):
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:create'),
            {
                'customer': 'Заказчик',
                'order_number': '300-9',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-201',
                'defects-TOTAL_FORMS': '1',
                'defects-INITIAL_FORMS': '0',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                **self._defect_post_fields(0, ''),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(Act.objects.filter(order_number='300-9').exists())
        self.assertContains(response, 'Обязательное поле.')

    def test_create_saves_multiple_defects_with_independent_workshop_values(self):
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:create'),
            {
                'customer': 'Заказчик',
                'order_number': '300-10',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-202',
                'defects-TOTAL_FORMS': '2',
                'defects-INITIAL_FORMS': '0',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                **self._defect_post_fields(0, ActDefect.Workshop.MP_SHOP),
                **self._defect_post_fields(1, ActDefect.Workshop.TRANSFORMERS_SHOP),
            },
        )

        self.assertEqual(response.status_code, 302)
        act = Act.objects.get(order_number='300-10')
        workshops = set(act.defects.values_list('workshop', flat=True))
        self.assertEqual(workshops, {ActDefect.Workshop.MP_SHOP, ActDefect.Workshop.TRANSFORMERS_SHOP})

    def test_edit_updates_defect_workshop_and_preserves_other_validation(self):
        act = self._create_act(self.status_created, created_by=self.otk_user)
        defect = ActDefect.objects.create(
            act=act, defect_type=self.defect_type, operation=self.operation,
            znp_number='1-1', party_number='2-2', mp_type='OL',
            checked_quantity=10, nonconforming_quantity=1,
            description='Исходное описание', detected_at=timezone.localdate(),
        )
        self.client.force_login(self.otk_user)

        response = self.client.post(
            reverse('acts:edit', args=[act.pk]),
            {
                'customer': 'Заказчик',
                'order_number': '100-3',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-103',
                'defects-TOTAL_FORMS': '1',
                'defects-INITIAL_FORMS': '1',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                'defects-0-id': defect.id,
                'defects-0-workshop': ActDefect.Workshop.TRANSFORMERS_SHOP,
                'defects-0-defect_type': self.defect_type.id,
                'defects-0-operation': self.operation.id,
                'defects-0-mp_type': 'OL',
                'defects-0-znp_number': '1-1',
                'defects-0-party_number': '2-2',
                'defects-0-checked_quantity': '10',
                'defects-0-nonconforming_quantity': '1',
                'defects-0-description': 'Обновлённое описание',
                'defects-0-detected_at': timezone.localdate().isoformat(),
            },
        )

        self.assertEqual(response.status_code, 302)
        defect.refresh_from_db()
        self.assertEqual(defect.workshop, ActDefect.Workshop.TRANSFORMERS_SHOP)
        self.assertEqual(defect.description, 'Обновлённое описание')

        # Existing quantity validation must still reject an invalid edit.
        response = self.client.post(
            reverse('acts:edit', args=[act.pk]),
            {
                'customer': 'Заказчик',
                'order_number': '100-3',
                'nomenclature': 'Катушка-А',
                'kd_designation': 'КД-103',
                'defects-TOTAL_FORMS': '1',
                'defects-INITIAL_FORMS': '1',
                'defects-MIN_NUM_FORMS': '1',
                'defects-MAX_NUM_FORMS': '1000',
                'defects-0-id': defect.id,
                'defects-0-workshop': ActDefect.Workshop.TRANSFORMERS_SHOP,
                'defects-0-defect_type': self.defect_type.id,
                'defects-0-operation': self.operation.id,
                'defects-0-mp_type': 'OL',
                'defects-0-znp_number': '1-1',
                'defects-0-party_number': '2-2',
                'defects-0-checked_quantity': '2',
                'defects-0-nonconforming_quantity': '5',
                'defects-0-description': 'Обновлённое описание',
                'defects-0-detected_at': timezone.localdate().isoformat(),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'не может превышать')

    def test_legacy_defect_without_workshop_displays_placeholder(self):
        act = self._create_act(self.status_to)
        ActDefect.objects.create(
            act=act, defect_type=self.defect_type, operation=self.operation,
            party_number='P-LEGACY', mp_type='OL', description='Легаси дефект',
            detected_at=timezone.localdate(),
        )
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Цех/')
        self.assertContains(response, '—')

    def test_otk_list_shows_only_own_created_otk_acts(self):
        visible = self._create_act(self.status_created, party_number='P-OTK')
        hidden_other = self._create_act(self.status_created, created_by=self.other_otk_user, party_number='P-OTHER')
        hidden_stage = self._create_act(self.status_ko, party_number='P-KO')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        self.assertContains(response, visible.number)
        self.assertNotContains(response, hidden_other.number)
        self.assertNotContains(response, hidden_stage.number)

    def test_registry_filters_by_status_act_type_and_search_without_operation_filter(self):
        matching = self._create_act(self.status_created, party_number='P-MATCH')
        incoming = self._create_act(
            self.status_created,
            party_number='P-OTHER',
            act_type=Act.Type.INCOMING_CONTROL,
        )
        self.client.force_login(self.otk_user)

        response = self.client.get(
            reverse('acts:list'),
            {'status': self.status_created.pk, 'act_type': Act.Type.OPERATIONAL_CONTROL, 'search': 'MATCH'},
        )

        self.assertContains(response, matching.number)
        self.assertNotContains(response, incoming.number)
        self.assertNotContains(response, 'name="operation"', html=False)
        self.assertNotIn('operations', response.context)
        self.assertNotIn('operation', response.context['selected'])
        self.assertNotContains(response, 'name="defect_type"', html=False)
        self.assertNotIn('defect_types', response.context)
        self.assertNotIn('defect_type', response.context['selected'])

    def test_registry_filter_options_match_current_workflow_and_act_types(self):
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        self.assertEqual(
            set(response.context['statuses'].values_list('code', flat=True)),
            {'CREATED_OTK', 'KO_REVIEW', 'TO_ANALYSIS', 'OTK_REVIEW', 'ARCHIVED'},
        )
        self.assertEqual(
            response.context['act_types'],
            Act.Type.choices,
        )

    def test_registry_table_displays_compact_act_columns(self):
        self._create_act(self.status_created, party_number='P-COMPACT')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        for header in ('Номер', 'Дата создания', 'Тип', 'Статус', 'Срок'):
            self.assertContains(response, f'<th>{header}</th>', html=False)
        for removed_header in ('Партия', 'Номенклатура', 'Операция', 'Вид дефекта', 'Приоритет', 'Создал'):
            self.assertNotContains(response, f'<th>{removed_header}</th>', html=False)

    def test_registry_ignores_removed_operation_query_parameter(self):
        visible = self._create_act(self.status_created, party_number='P-VISIBLE')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'), {'operation': self.operation.pk})

        self.assertContains(response, visible.number)
        self.assertFalse(response.context['has_filters'])

    def test_registry_due_filter_treats_only_dates_before_today_as_overdue(self):
        today = timezone.localdate()
        overdue = self._create_act(self.status_created, party_number='P-OVERDUE', due_date=today - timedelta(days=1))
        due_today = self._create_act(self.status_created, party_number='P-TODAY', due_date=today)
        future = self._create_act(self.status_created, party_number='P-FUTURE', due_date=today + timedelta(days=1))
        self.client.force_login(self.otk_user)

        overdue_response = self.client.get(reverse('acts:list'), {'due': 'overdue'})
        self.assertContains(overdue_response, overdue.number)
        self.assertNotContains(overdue_response, due_today.number)
        self.assertNotContains(overdue_response, future.number)

        not_overdue_response = self.client.get(reverse('acts:list'), {'due': 'not_overdue'})
        self.assertNotContains(not_overdue_response, overdue.number)
        self.assertContains(not_overdue_response, due_today.number)
        self.assertContains(not_overdue_response, future.number)

    def test_registry_reset_retains_scope_and_clears_remaining_filters(self):
        self._create_act(self.status_created, party_number='P-RESET')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'), {'scope': 'all', 'search': 'RESET'})

        self.assertContains(response, '?scope=all')
        self.assertEqual(response.context['selected']['search'], 'RESET')

        reset_response = self.client.get(reverse('acts:list'), {'scope': 'all'})
        self.assertEqual(reset_response.context['scope'], 'all')
        self.assertEqual(reset_response.context['selected']['search'], '')
        self.assertFalse(reset_response.context['has_filters'])

    def test_registry_create_button_respects_create_permission(self):
        self.client.force_login(self.otk_user)
        response = self.client.get(reverse('acts:list'))
        self.assertEqual(response.context['header_title'], 'Акты')
        self.assertContains(response, 'Создать АКТ')

        self.client.force_login(self.ko_user)
        self.assertNotContains(self.client.get(reverse('acts:list')), 'Создать АКТ')

    def test_registry_cleanup_control_is_visible_only_to_dedicated_administrator(self):
        dedicated_admin = self._create_user('admin_user', UserProfile.Role.ADMIN)

        self.client.force_login(self.admin_user)
        self.assertNotContains(self.client.get(reverse('acts:list')), 'Очистить акты')

        self.client.force_login(dedicated_admin)
        self.assertContains(self.client.get(reverse('acts:list')), 'Очистить акты')

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

    def test_cleanup_removes_tasks_that_protect_approved_acts(self):
        act = self._create_act(self.status_archived, party_number='P-CLEAR-TASK')
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина очистки')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие очистки', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        task = Task.objects.create(
            source_action=action, act=act, root_analysis=root, task_text=action.comment,
            department=self.department, due_date=timezone.localdate(), created_by=self.otk_user,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=task, user=self.to_user)
        dedicated_admin = self._create_user('admin_user', UserProfile.Role.ADMIN)

        self.client.force_login(dedicated_admin)
        response = self.client.post(reverse('acts:clear_all'))

        self.assertRedirects(response, reverse('acts:list'))
        self.assertFalse(Act.objects.exists())
        self.assertFalse(Task.objects.exists())

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

    def test_ko_decision_form_has_required_choices_in_order(self):
        act = self._create_act(self.status_ko)
        self.client.force_login(self.ko_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertEqual(
            list(response.context['ko_decision_form'].fields['ko_decision'].choices),
            [
                (Act.KoDecision.PROHIBIT_USE, 'Запретить использование'),
                (
                    Act.KoDecision.ALLOW_NO_REWORK,
                    'Разрешить использование изделия с отклонением без доработки',
                ),
                (
                    Act.KoDecision.ALLOW_WITH_REWORK,
                    'Разрешить использование изделия с отклонением с доработкой',
                ),
                (
                    Act.KoDecision.ALLOW_NO_DEVIATION_REWORK,
                    'Разрешить использование изделия без отклонения с доработкой',
                ),
            ],
        )

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
            {
                'action': 'send_to_otk',
                'root-TOTAL_FORMS': '1',
                'root-0-root_cause': 'Причина',
                'root-0-actions-TOTAL_FORMS': '1',
                'root-0-actions-0-comment': 'Мероприятия',
                'root-0-actions-0-department': str(self.department.pk),
                'root-0-actions-0-assignees': [str(self.to_user.pk)],
                'root-0-actions-0-due_date': timezone.localdate().isoformat(),
            },
        )

        self.assertRedirects(response, reverse('acts:list'))
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertEqual(self.client.get(reverse('acts:detail', args=[act.pk])).status_code, 404)

    def test_to_analysis_form_is_embedded_on_work_tab(self):
        act = self._create_act(self.status_to)
        self.client.force_login(self.to_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertContains(response, 'На проверку ОТК')
        self.assertContains(response, 'Вернуть КО')
        self.assertNotContains(response, 'Сохранить анализ ТО')
        self.assertNotContains(response, 'Внести анализ ТО')
        self.assertNotContains(response, '<button class="link-button link-button--danger" type="button" data-remove-root-analysis>')
        self.assertNotContains(response, '<button class="link-button link-button--danger" type="button" data-remove-corrective-action>')
        self.assertContains(response, 'link-button--success')
        self.assertContains(response, 'name="root-0-actions-TOTAL_FORMS" value="1"')
        self.assertContains(response, 'data-root-analysis-title')
        self.assertContains(response, 'Корневая причина 1')
        self.assertContains(response, 'class="corrective-action-row"')
        self.assertContains(response, 'rows="2"')

    def test_otk_sees_own_act_at_otk_review_stage(self):
        act = self._create_act(self.status_otk_review, created_by=self.otk_user)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:list'))

        self.assertContains(response, act.number)

    def test_otk_review_shows_otk_actions_only_to_authorized_user(self):
        act = self._create_act(self.status_otk_review, created_by=self.otk_user)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertContains(response, 'Вернуть ТО')
        self.assertContains(response, 'Утвердить')

    def test_registry_scopes_keep_archived_act_permission_safe(self):
        active_act = self._create_act(self.status_created, created_by=self.otk_user, party_number='P-ACTIVE')
        archived_act = self._create_act(self.status_otk_review, created_by=self.otk_user, party_number='P-ARCHIVE')
        archived_act.status = self.status_archived
        archived_act.approved_by = self.otk_user
        archived_act.save(update_fields=['status', 'approved_by', 'updated_at'])
        self.client.force_login(self.otk_user)

        my_response = self.client.get(reverse('acts:list') + '?scope=my')
        archive_response = self.client.get(reverse('acts:list') + '?scope=archive')

        self.assertContains(my_response, active_act.number)
        self.assertNotContains(my_response, archived_act.number)
        self.assertContains(archive_response, archived_act.number)
        self.assertNotContains(archive_response, active_act.number)

    def test_full_access_user_sees_archived_acts_only_on_archive_tab(self):
        active_act = self._create_act(self.status_created, party_number='P-ADMIN-ACTIVE')
        archived_act = self._create_act(self.status_archived, party_number='P-ADMIN-ARCHIVE')
        self.client.force_login(self.admin_user)

        my_response = self.client.get(reverse('acts:list') + '?scope=my')
        all_response = self.client.get(reverse('acts:list') + '?scope=all')
        archive_response = self.client.get(reverse('acts:list') + '?scope=archive')

        for response in (my_response, all_response):
            self.assertContains(response, active_act.number)
            self.assertNotContains(response, archived_act.number)
        self.assertContains(archive_response, archived_act.number)
        self.assertNotContains(archive_response, active_act.number)

    def test_archive_registry_shows_archiving_date(self):
        archived_at = timezone.now()
        archived_act = self._create_act(self.status_archived, party_number='P-ARCHIVE-DATE')
        archived_act.approved_at = archived_at
        archived_act.save(update_fields=['approved_at', 'updated_at'])
        self.client.force_login(self.admin_user)

        response = self.client.get(reverse('acts:list') + '?scope=archive')

        self.assertContains(response, '<th>Дата архивации</th>', html=False)
        self.assertContains(response, timezone.localtime(archived_at).strftime('%d.%m.%Y'))
        self.assertNotContains(response, '<th>Дата создания</th>', html=False)

    def test_legacy_to_analysis_values_remain_visible_without_structured_records(self):
        act = self._create_act(self.status_actions)
        act.to_root_cause = 'Историческая причина'
        act.to_action_summary = 'Историческое мероприятие'
        act.save(update_fields=['to_root_cause', 'to_action_summary', 'updated_at'])
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertContains(response, 'Историческая причина')
        self.assertContains(response, 'Историческое мероприятие')

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
        self.assertNotContains(response, 'Режим администратора: полный доступ к актам.')

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

    def test_detail_has_four_tabs_and_keeps_attachment_tab(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        for label in ('Проработка', 'История акта', 'Вложения и комментарии', 'Связанные мероприятия'):
            self.assertContains(response, label)
        self.assertContains(self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=attachments'), 'Вложения')

    def test_attachment_tab_uses_compact_collaboration_layout(self):
        act = self._create_act(self.status_created)
        ActComment.objects.create(act=act, author=self.otk_user, text='Comment for feed')
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=attachments')

        self.assertContains(response, 'act-collaboration-layout')
        self.assertContains(response, 'attachment-picker')
        self.assertContains(response, 'data-attachment-file-trigger')
        self.assertContains(response, 'comment-card__avatar')

    def test_detail_defects_table_is_compact_and_ko_is_read_only_after_transfer(self):
        act = self._create_act(self.status_to)
        defect = ActDefect.objects.create(
            act=act, defect_type=self.defect_type, operation=self.operation, party_number='P-DETAIL',
            mp_type='OL', checked_quantity=25, nonconforming_quantity=3, description='Описание',
            detected_at=timezone.localdate(), ko_decision=Act.KoDecision.PROHIBIT_USE, ko_comment='Комментарий КО',
        )
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        for header in (
            '№', 'Номер<br>партии', 'Вид дефекта', 'Тип МП', 'Дата<br>обнаружения',
            'Всего<br>проверено', 'С<br>отклонением', 'Описание', 'Решение КО', 'Комментарий КО',
        ):
            self.assertContains(response, f'<th>{header}</th>', html=False)
        self.assertContains(response, '<colgroup>', html=False)
        for column_class in (
            'act-defects-table__description',
            'act-defects-table__decision',
            'act-defects-table__comment',
        ):
            self.assertContains(response, column_class, html=False)
        self.assertContains(response, defect.get_ko_decision_display())
        self.assertContains(response, 'Комментарий КО')
        self.assertNotContains(response, 'процент')
        self.assertNotContains(response, 'form="ko-decision-form"', html=False)

    def test_ko_decision_controls_remain_editable_during_ko_review(self):
        act = self._create_act(self.status_ko)
        ActDefect.objects.create(
            act=act, defect_type=self.defect_type, operation=self.operation, party_number='P-KO-DETAIL',
            mp_type='OL', description='Описание', detected_at=timezone.localdate(),
        )
        self.client.force_login(self.ko_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertContains(response, 'form="ko-decision-form"', html=False)

    def test_related_activities_only_include_tasks_visible_to_user(self):
        act = self._create_act(self.status_archived)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Видимое мероприятие', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        task = Task.objects.create(
            source_action=action, act=act, root_analysis=root, task_text=action.comment,
            department=self.department, due_date=timezone.localdate(), created_by=self.otk_user,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=task, user=self.to_user)

        self.client.force_login(self.manager_user)
        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=activities')
        self.assertContains(response, 'Видимое мероприятие')
        self.assertContains(response, '<th>№ задачи</th>', html=False)
        self.assertContains(response, '<th>Причина</th>', html=False)
        self.assertNotContains(response, '<th>Тип</th>', html=False)
        self.assertNotContains(response, '<th>Отдел</th>', html=False)
        self.assertContains(response, 'related-activities__table')
        self.assertContains(response, reverse('tasks:detail', args=[task.pk]))

        self.client.force_login(self.otk_user)
        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=activities')
        self.assertNotContains(response, 'Видимое мероприятие')

    def test_detail_uses_corporate_header_and_compact_route(self):
        act = self._create_act(self.status_created)
        self.client.force_login(self.otk_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]))

        self.assertContains(response, 'Экосистема качества')
        self.assertContains(response, 'Пользователи и роли')
        self.assertContains(response, f'Акт {act.number}')
        self.assertContains(response, 'Текущий этап')
        self.assertContains(response, 'Ожидает')

    def test_readonly_to_analysis_compacts_columns_and_lists_multiple_assignees(self):
        act = self._create_act(self.status_otk_review)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Плохое оснащение участка')
        single_action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Проверить оснастку', department=self.department,
            due_date=timezone.localdate(),
        )
        multiple_action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Доработать приспособление', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=single_action, user=self.to_user)
        ActCorrectiveActionAssignee.objects.create(corrective_action=multiple_action, user=self.to_user)
        ActCorrectiveActionAssignee.objects.create(corrective_action=multiple_action, user=self.other_otk_user)
        task = Task.objects.create(
            source_action=multiple_action, act=act, root_analysis=root, task_text=multiple_action.comment,
            department=self.department, due_date=timezone.localdate(), created_by=self.otk_user,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=task, user=self.to_user)
        TaskAssignee.objects.create(task=task, user=self.other_otk_user)
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        for header in ('№', 'Мероприятие', 'Исполнитель(и)', 'Срок', 'Статус'):
            self.assertContains(response, f'<th>{header}</th>', html=False)
        self.assertNotContains(response, '<th>Тип</th>', html=False)
        self.assertNotContains(response, '<th>Отдел</th>', html=False)
        self.assertContains(response, 'to-analysis-section')
        self.assertContains(response, 'to-analysis-readonly__cause')
        self.assertContains(response, 'Плохое оснащение участка')
        self.assertContains(response, 'to-analysis-assignee-list')
        self.assertNotContains(response, 'to-analysis-assignees"')
        self.assertContains(response, self.to_user.username)
        self.assertContains(response, self.other_otk_user.username)
        self.assertNotContains(response, '<select', html=False)
        self.assertNotContains(response, 'Будет создана после утверждения')
        self.assertContains(response, str(task.status))

    def test_readonly_to_analysis_shows_single_assignee_without_expand_control(self):
        act = self._create_act(self.status_otk_review)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'to-analysis-assignee-list')
        self.assertContains(response, self.to_user.username)
        self.assertNotContains(response, 'to-analysis-assignees"')

    def test_readonly_to_analysis_shows_dash_when_action_has_no_assignees(self):
        act = self._create_act(self.status_archived)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие без исполнителей', department=self.department,
            due_date=timezone.localdate(),
        )
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Мероприятие без исполнителей')
        self.assertNotContains(response, 'to-analysis-assignee-list')
        self.assertContains(response, '—')

    def test_readonly_to_analysis_on_archived_act_lists_assignees_without_select(self):
        act = self._create_act(self.status_archived)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина архива')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Архивное мероприятие', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.other_otk_user)
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Архивное мероприятие')
        self.assertContains(response, 'to-analysis-assignee-list')
        self.assertContains(response, self.to_user.username)
        self.assertContains(response, self.other_otk_user.username)
        self.assertNotContains(response, '<select', html=False)

    def test_to_analysis_edit_form_keeps_assignee_select_for_editable_status(self):
        act = self._create_act(self.status_to)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие', department=self.department,
            due_date=timezone.localdate() + timedelta(days=3),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        self.client.force_login(self.to_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-assignees-select')
        self.assertContains(response, f'value="{self.to_user.pk}" data-department-id')
        self.assertNotContains(response, 'to-analysis-assignee-list')

    def test_readonly_to_analysis_assignee_display_does_not_scale_queries_per_assignee(self):
        act = self._create_act(self.status_otk_review)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root, comment='Мероприятие', department=self.department,
            due_date=timezone.localdate(),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)
        self.client.force_login(self.manager_user)
        url = reverse('acts:detail', args=[act.pk]) + '?tab=work'

        with CaptureQueriesContext(connection) as single_assignee_queries:
            self.client.get(url)

        extra_assignees = [
            self._create_user(f'to_extra_{i}', UserProfile.Role.TO) for i in range(4)
        ]
        for extra_user in extra_assignees:
            ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=extra_user)

        with CaptureQueriesContext(connection) as many_assignees_queries:
            response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        for extra_user in extra_assignees:
            self.assertContains(response, extra_user.username)
        self.assertEqual(len(single_assignee_queries), len(many_assignees_queries))

    def test_archived_analysis_does_not_repeat_approval_metadata(self):
        act = self._create_act(self.status_archived)
        self.client.force_login(self.manager_user)

        response = self.client.get(reverse('acts:detail', args=[act.pk]) + '?tab=work')

        self.assertNotContains(response, '<dt>Утвердил</dt>', html=False)
        self.assertNotContains(response, '<dt>Дата утверждения</dt>', html=False)
        self.assertEqual(response.context['route_steps'][-1]['state'], 'completed')
