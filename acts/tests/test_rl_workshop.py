"""РЛ decisions and analysis are one transaction; ТО access is per workshop."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from accounts.admin import UserProfileAdminForm
from accounts.models import Department, RoleSubstitution, UserProfile
from acts import workshops
from acts.forms import ActDefectForm, ActDefectEditFormSet
from acts.models import Act, ActDefect
from acts.permissions import (
    can_apply_to_analysis, can_view_act, get_all_visible_acts_queryset,
    get_archived_acts_queryset, get_visible_acts_queryset,
)
from acts.print_form import build_act_print_form
from acts.selectors import build_route_steps
from acts.services import (
    ActWorkflowError, apply_structured_to_analysis, apply_to_analysis,
    approve_act, return_to_ko, return_to_otk, return_to_to, send_to_ko,
)
from references.models import ActStatus, DefectType, Operation
from tasks.models import Task
from tasks.services import add_substitute_to_open_act_workflow_tasks


DECISION = Act.KoDecision.ALLOW_NO_DEVIATION_REWORK


class RlWorkshopTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command('seed_references', stdout=StringIO())
        cls.department = Department.objects.create(code='RL_TEST', name='ТО')
        for name, role in (
            ('otk', UserProfile.Role.OTK), ('head', UserProfile.Role.TO),
            ('mp', UserProfile.Role.TO_MP), ('rl', UserProfile.Role.TO_RL),
            ('tr', UserProfile.Role.TO_TR), ('ko', UserProfile.Role.KO),
            ('master', UserProfile.Role.MAS_MP_RL),
        ):
            user = User.objects.create_user(f'rl_{name}')
            user.userprofile.role = role
            user.userprofile.department = cls.department
            user.userprofile.save()
            setattr(cls, name, User.objects.get(pk=user.pk))
        cls.defect_type = DefectType.objects.get(code='SIZE_NONCONFORMITY')

    def make_act(self, workshop=workshops.RL_SHOP, status='CREATED_OTK', count=2):
        act = Act.objects.create(
            created_by=self.otk, nomenclature='Магнитопровод',
            status=ActStatus.objects.get(code=status), due_date=timezone.localdate(),
        )
        for _index in range(count):
            ActDefect.objects.create(
                act=act, workshop=workshop, defect_type=self.defect_type,
                znp_number='123-4', checked_quantity=10, nonconforming_quantity=2,
                detected_at=timezone.localdate(), description='Заусенец на кромке',
            )
        return act

    def analysis(self):
        return [{'root_cause': 'Износ инструмента', 'actions': [{
            'comment': 'Заменить инструмент', 'department': self.department,
            'due_date': timezone.localdate(), 'assignees': [self.rl],
        }]}]

    def decisions(self, act):
        return [(defect, DECISION, 'Обработать кромку', {}) for defect in act.defects.all()]

    def submit(self, act, user=None):
        return apply_structured_to_analysis(
            act, user or self.rl, self.analysis(), defect_decisions=self.decisions(act),
        )

    def payload(self, act):
        data = {
            'action': 'send_to_otk', 'root-TOTAL_FORMS': '1',
            'root-0-root_cause': 'Износ инструмента',
            'root-0-actions-TOTAL_FORMS': '1',
            'root-0-actions-0-comment': 'Заменить инструмент',
            'root-0-actions-0-department': str(self.department.pk),
            'root-0-actions-0-assignees': [str(self.rl.pk)],
            'root-0-actions-0-due_date': timezone.localdate().isoformat(),
            'form-TOTAL_FORMS': str(act.defects.count()),
            'form-INITIAL_FORMS': str(act.defects.count()),
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        for index, defect in enumerate(act.defects.all()):
            data.update({f'form-{index}-id': str(defect.pk),
                         f'form-{index}-ko_decision': DECISION,
                         f'form-{index}-ko_comment': 'Обработать кромку'})
        return data

    def routing_ids(self, act):
        task = act.tasks.get(source_type=Task.SourceType.ACT_WORKFLOW, status__code='IN_PROGRESS')
        return set(task.assignees.values_list('user_id', flat=True))

    def test_profile_validates_all_ten_types_and_clears_mp_fields(self):
        data = {
            'workshop': workshops.RL_SHOP, 'znp_number': '123-4',
            'checked_quantity': 10, 'nonconforming_quantity': 2,
            'detected_at': timezone.localdate().isoformat(), 'description': 'Описание',
            'party_number': '999', 'mp_type': 'OL',
            'operation': Operation.objects.get(code='NAVIVKA').pk,
        }
        for code, _label in workshops.RL_DEFECT_TYPES:
            with self.subTest(code=code):
                form = ActDefectForm({**data, 'defect_type': DefectType.objects.get(code=code).pk})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data['party_number'], '')
                self.assertEqual(form.cleaned_data['mp_type'], '')
                self.assertIsNone(form.cleaned_data['operation'])
        form = ActDefectForm({**data, 'defect_type': DefectType.objects.get(code='DEFORMATION').pk})
        self.assertFalse(form.is_valid())
        self.assertIn('defect_type', form.errors)
        self.assertNotIn(workshops.TR_SHOP, dict(ActDefect.Workshop.choices))

    def test_database_rejects_mp_only_data_on_rl(self):
        defect = self.make_act().defects.first()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ActDefect.objects.filter(pk=defect.pk).update(party_number='1')

    def create_payload(self, count=3):
        data = {
            'number_suffix': '42', 'customer': 'Заказчик', 'order_number': '123-4',
            'nomenclature': 'Магнитопровод', 'kd_designation': 'КД-42',
            'defects-TOTAL_FORMS': str(count), 'defects-INITIAL_FORMS': '0',
            'defects-MIN_NUM_FORMS': '1', 'defects-MAX_NUM_FORMS': '1000',
        }
        for index in range(count):
            data.update({
                f'defects-{index}-workshop': workshops.RL_SHOP,
                f'defects-{index}-znp_number': f'123-{index}',
                f'defects-{index}-defect_type': str(self.defect_type.pk),
                f'defects-{index}-detected_at': timezone.localdate().isoformat(),
                f'defects-{index}-checked_quantity': '10',
                f'defects-{index}-nonconforming_quantity': '2',
                f'defects-{index}-description': 'Заусенцы',
            })
        return data

    def test_creation_stores_three_rl_defects_and_rejects_a_missing_first_workshop(self):
        self.client.force_login(self.otk)
        response = self.client.post(reverse('acts:create'), self.create_payload())
        self.assertEqual(response.status_code, 302)
        act = Act.objects.get(order_number='123-4')
        self.assertEqual(list(act.defects.values_list('workshop', flat=True)), [workshops.RL_SHOP] * 3)
        data = self.create_payload()
        data['defects-0-workshop'] = ''
        response = self.client.post(reverse('acts:create'), data)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Act.objects.count(), 1)

    def test_edit_keeps_legacy_workshops_but_new_defects_must_inherit(self):
        act = self.make_act(workshop=workshops.PIR_SHOP, count=0)
        for code in (workshops.PIR_SHOP, workshops.RL_SHOP):
            ActDefect.objects.create(
                act=act, workshop=code, defect_type=self.defect_type,
                detected_at=timezone.localdate(),
            )
        data = self.create_payload(2)
        defects = list(act.defects.all())
        data['defects-INITIAL_FORMS'] = '2'
        for index, defect in enumerate(defects):
            data[f'defects-{index}-id'] = str(defect.pk)
            data[f'defects-{index}-workshop'] = defect.workshop
        formset = ActDefectEditFormSet(data, instance=act)
        self.assertTrue(formset.is_valid(), formset.errors)
        data['defects-TOTAL_FORMS'] = '3'
        data.update({key: value for key, value in self.create_payload(3).items()
                     if key.startswith('defects-2-')})
        formset = ActDefectEditFormSet(data, instance=act)
        self.assertFalse(formset.is_valid())
        self.assertIn('workshop', formset.errors[2])

    def test_all_four_to_roles_are_offered_in_admin(self):
        choices = dict(UserProfileAdminForm().fields['role'].choices)
        self.assertEqual(choices['to'], 'Начальник ТО')
        self.assertEqual([choices[code] for code in ('to_mp', 'to_rl', 'to_tr')],
                         ['Технолог МП', 'Технолог РЛ', 'Технолог ТР'])

    def test_technologists_read_and_work_only_their_workshops(self):
        rl = self.make_act(status='TO_ANALYSIS')
        mp = self.make_act(workshop=workshops.MP_SHOP, status='TO_ANALYSIS')
        pir = self.make_act(workshop='', status='TO_ANALYSIS')
        for user, expected in ((self.rl, {rl}), (self.mp, {mp}), (self.tr, set()),
                               (self.head, {rl, mp, pir})):
            with self.subTest(user=user.pk):
                self.assertEqual(set(get_visible_acts_queryset(user)), expected)
                self.assertEqual(set(get_all_visible_acts_queryset(user)), expected)
                for act in (rl, mp, pir):
                    self.assertEqual(can_view_act(act, user), act in expected)
                    self.assertEqual(can_apply_to_analysis(act, user), act in expected)
        mp.status = ActStatus.objects.get(code='ARCHIVED')
        mp.save()
        self.assertEqual(set(get_archived_acts_queryset(self.mp)), {mp})
        self.assertFalse(get_archived_acts_queryset(self.rl).exists())

    def test_foreign_act_endpoints_are_not_readable(self):
        act = self.make_act(workshop=workshops.MP_SHOP)
        self.client.force_login(self.rl)
        for name in ('detail', 'print', 'work_fragment', 'history_fragment', 'activities_fragment'):
            with self.subTest(name=name):
                self.assertEqual(self.client.get(reverse(f'acts:{name}', args=[act.pk])).status_code, 404)

    def test_route_skips_ko_and_addresses_only_rl_and_head(self):
        act = send_to_ko(self.make_act(), self.otk)
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(self.routing_ids(act), {self.rl.pk, self.head.pk})
        self.assertFalse(act.history_events.filter(event_type='SENT_TO_KO').exists())
        names = [step['name'] for step in build_route_steps(act)]
        self.assertEqual(names, ['Создан ОТК', 'Проработка ТО', 'Проверка ОТК', 'Архивирован'])
        self.assertEqual(build_route_steps(act)[1]['state'], 'current')
        self.assertTrue(self.rl.notifications.filter(related_act=act).exists())
        self.assertFalse(self.mp.notifications.filter(related_act=act).exists())
        self.assertFalse(self.ko.notifications.filter(related_act=act).exists())

    def test_work_page_shares_one_form_and_existing_cards(self):
        act = send_to_ko(self.make_act(), self.otk)
        self.client.force_login(self.rl)
        response = self.client.get(reverse('acts:detail', args=[act.pk]))
        self.assertContains(response, 'Проработка ТО')
        self.assertContains(response, 'act-defect-card__work')
        self.assertContains(response, 'name="form-0-ko_decision"')
        self.assertContains(response, 'form="to-analysis-form"')
        self.assertNotContains(response, 'id="ko-decision-form"')
        self.assertNotContains(response, '>Вернуть КО</button>')

    def test_complete_post_saves_decisions_analysis_print_and_approval(self):
        act = send_to_ko(self.make_act(), self.otk)
        self.client.force_login(self.rl)
        response = self.client.post(reverse('acts:to_analysis', args=[act.pk]), self.payload(act))
        self.assertEqual(response.status_code, 302)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertEqual(act.to_analysis_by, self.rl)
        self.assertEqual(set(act.defects.values_list('ko_decision_by_id', flat=True)), {self.rl.pk})
        self.assertEqual(act.root_analyses.count(), 1)
        self.assertEqual(self.routing_ids(act), {self.otk.pk})
        printed = build_act_print_form(act)
        self.assertEqual(len(printed['forms']), 2)
        for form in printed['forms']:
            self.assertEqual(form['ko_signature']['name'], self.rl.username)
            self.assertEqual(form['ko_signature']['position'], 'Технолог РЛ')
        self.assertEqual(printed['shared']['to_signature']['position'], 'Технолог РЛ')
        print_task = act.tasks.get(source_type=Task.SourceType.ACT_PRINT)
        self.assertEqual(set(print_task.assignees.values_list('user_id', flat=True)),
                         {self.otk.pk, self.master.pk})
        act = approve_act(act, self.otk)
        self.assertEqual(act.status.code, 'ARCHIVED')
        self.assertEqual(act.tasks.filter(source_type=Task.SourceType.ACT).count(), 1)

    def test_invalid_analysis_and_decision_keep_input_and_write_nothing(self):
        act = send_to_ko(self.make_act(), self.otk)
        self.client.force_login(self.rl)
        data = self.payload(act)
        data['root-0-root_cause'] = ''
        data['form-1-ko_decision'] = ''
        response = self.client.post(reverse('acts:to_analysis', args=[act.pk]), data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Заменить инструмент')
        self.assertContains(response, 'Обработать кромку')
        self.assertFalse(act.defects.exclude(ko_decision='').exists())
        self.assertFalse(act.root_analyses.exists())
        self.assertEqual(self.routing_ids(act), {self.rl.pk, self.head.pk})

    def test_direct_service_rejects_missing_duplicate_foreign_and_invalid_decisions(self):
        act = send_to_ko(self.make_act(), self.otk)
        foreign = self.make_act().defects.first()
        decisions = self.decisions(act)
        for rows in ([], decisions[:1], [decisions[0], decisions[0]],
                     [(foreign, DECISION, '', {}), decisions[1]],
                     [(d, 'INVALID', '', {}) for d, _v, _c, _i in decisions],
                     [(d, Act.KoDecision.ALLOW_NO_REWORK, '', {}) for d, _v, _c, _i in decisions]):
            with self.subTest(rows=repr(rows)), self.assertRaises(ActWorkflowError):
                apply_structured_to_analysis(act, self.rl, self.analysis(), defect_decisions=rows)
            self.assertFalse(act.defects.exclude(ko_decision='').exists())
        with self.assertRaises(ActWorkflowError):
            apply_to_analysis(act, self.rl, 'Причина', 'Действие')

    def test_wrong_workshop_and_ko_cannot_submit_combined_work(self):
        act = send_to_ko(self.make_act(), self.otk)
        for user in (self.mp, self.tr, self.ko):
            with self.subTest(user=user.pk), self.assertRaises(ActWorkflowError):
                self.submit(act, user)

    def test_analysis_failure_rolls_back_decisions_history_and_tasks(self):
        act = send_to_ko(self.make_act(), self.otk)
        count = act.history_events.count()
        with patch('acts.services.ActRootAnalysis.objects.create', side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):
                self.submit(act)
        self.assertEqual(act.history_events.count(), count)
        self.assertFalse(act.defects.exclude(ko_decision='').exists())
        self.assertEqual(self.routing_ids(act), {self.rl.pk, self.head.pk})

    def test_returns_keep_rl_on_its_own_route_and_replay_is_refused(self):
        act = send_to_ko(self.make_act(), self.otk)
        with self.assertRaises(ActWorkflowError):
            return_to_ko(act, self.rl, 'Уточнить')
        act = return_to_otk(act, self.rl, 'Уточнить данные')
        self.assertEqual(act.status.code, 'CREATED_OTK')
        act = send_to_ko(act, self.otk)
        act = self.submit(act)
        with self.assertRaises(ActWorkflowError):
            self.submit(act)
        act = return_to_to(act, self.otk, 'Уточнить решение')
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(self.routing_ids(act), {self.rl.pk, self.head.pk})
        self.assertTrue(all(d.ko_round < act.ko_round for d in act.defects.all()))
        act = self.submit(act, self.head)
        self.assertEqual(set(act.defects.values_list('ko_decision_by_id', flat=True)), {self.head.pk})

    def test_substitution_adds_only_the_matching_workshop_queue(self):
        rl = send_to_ko(self.make_act(), self.otk)
        mp = self.make_act(workshop=workshops.MP_SHOP, status='TO_ANALYSIS')
        # An existing ТО МП holder substitutes for РЛ, retaining their МП scope.
        RoleSubstitution.objects.create(
            user=self.mp, role=UserProfile.Role.TO_RL,
            date_from=timezone.localdate(), date_to=timezone.localdate() + timedelta(days=1),
            substitutes_for=self.rl,
        )
        user = User.objects.get(pk=self.mp.pk)
        add_substitute_to_open_act_workflow_tasks(user, UserProfile.Role.TO_RL)
        self.assertEqual(set(get_visible_acts_queryset(user)), {rl, mp})
        self.assertIn(user.pk, self.routing_ids(rl))
        self.submit(rl, user)
        notes = rl.history_events.filter(
            event_type__in=['KO_DECISION_APPLIED', 'TO_ANALYSIS_APPLIED'],
        ).values_list('substitution_note', flat=True)
        self.assertEqual(set(notes), {f'замещает {self.rl.username}'})

    def test_forwarding_rl_in_a_lent_otk_role_records_the_actual_role(self):
        RoleSubstitution.objects.create(
            user=self.mp, role=UserProfile.Role.OTK,
            date_from=timezone.localdate(), date_to=timezone.localdate(),
            substitutes_for=self.otk,
        )
        user = User.objects.get(pk=self.mp.pk)
        act = self.make_act()
        act.created_by = user
        act.save()
        act = send_to_ko(act, user)
        self.assertEqual(act.history_events.get(event_type='SENT_TO_TO').substitution_note,
                         f'замещает {self.otk.username}')


@skipUnlessDBFeature('has_select_for_update')
class RlConcurrentSubmissionTests(TransactionTestCase):
    def test_two_submissions_persist_one_complete_workup(self):
        from acts.tests.test_concurrency import run_in_parallel

        call_command('seed_references', stdout=StringIO())
        department = Department.objects.create(code='RL_CONCURRENT', name='ТО')
        users = []
        for name, role in (('rl_concurrent_otk', UserProfile.Role.OTK),
                           ('rl_concurrent_to', UserProfile.Role.TO_RL)):
            user = User.objects.create_user(name)
            user.userprofile.role = role
            user.userprofile.department = department
            user.userprofile.save()
            users.append(user)
        otk, technologist = users
        act = Act.objects.create(
            created_by=otk, status=ActStatus.objects.get(code='CREATED_OTK'),
            due_date=timezone.localdate(),
        )
        for _index in range(2):
            ActDefect.objects.create(
                act=act, workshop=workshops.RL_SHOP,
                defect_type=DefectType.objects.get(code='BURRS'),
                detected_at=timezone.localdate(),
            )
        act = send_to_ko(act, otk)

        def submit():
            # Each request owns its own model and role cache.
            user = User.objects.get(pk=technologist.pk)
            return apply_structured_to_analysis(
                act.pk, user,
                [{'root_cause': 'Причина', 'actions': [{
                    'comment': 'Мероприятие', 'department': department,
                    'due_date': timezone.localdate(), 'assignees': [user],
                }]}],
                defect_decisions=[(d, DECISION, '', {}) for d in act.defects.all()],
            )

        results = run_in_parallel([submit, submit])
        self.assertEqual(sorted(outcome for outcome, _value in results), ['error', 'ok'])
        self.assertIsInstance(next(value for outcome, value in results if outcome == 'error'), ActWorkflowError)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertEqual(act.root_analyses.count(), 1)
        self.assertEqual(act.history_events.filter(event_type='KO_DECISION_APPLIED').count(), 2)
        self.assertEqual(act.history_events.filter(event_type='TO_ANALYSIS_APPLIED').count(), 1)
        self.assertEqual(act.tasks.filter(source_type=Task.SourceType.ACT_WORKFLOW,
                                         status__code='IN_PROGRESS').count(), 1)
        self.assertEqual(act.tasks.filter(source_type=Task.SourceType.ACT_PRINT).count(), 1)
