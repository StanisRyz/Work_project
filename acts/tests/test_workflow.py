from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.forms import ToAnalysisStructureForm
from acts.models import Act, ActComment, ActCorrectiveAction, ActDefect, ActHistoryEvent, ActRootAnalysis
from acts.services import ActWorkflowError, apply_ko_decision, apply_structured_to_analysis, apply_to_analysis, approve_act, return_to_ko, return_to_otk, return_to_to, send_to_ko
from references.models import ActStatus, DefectType, Operation
from tasks.models import Task


class ActWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_created = ActStatus.objects.create(code='CREATED_OTK', name='Создан ОТК')
        cls.status_ko = ActStatus.objects.create(code='KO_REVIEW', name='На рассмотрении КО')
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_actions = ActStatus.objects.create(code='ACTIONS_ASSIGNED', name='Мероприятия назначены')
        cls.status_otk_review = ActStatus.objects.get(code='OTK_REVIEW')
        cls.status_archived = ActStatus.objects.get(code='ARCHIVED')
        cls.operation = Operation.objects.create(code='OP', name='Операция')
        cls.defect_type = DefectType.objects.create(code='DEFECT', name='Дефект')
        cls.department = Department.objects.create(code='TO', name='Технологический отдел')
        cls.other_department = Department.objects.create(code='OTHER', name='Другой отдел')

        cls.otk_user = cls._create_user('otk', UserProfile.Role.OTK)
        cls.ko_user = cls._create_user('ko', UserProfile.Role.KO)
        cls.to_user = cls._create_user('to', UserProfile.Role.TO)
        cls.to_user.userprofile.department = cls.department
        cls.to_user.userprofile.save()
        cls.other_user = cls._create_user('other', UserProfile.Role.TO)
        cls.other_user.userprofile.department = cls.other_department
        cls.other_user.userprofile.save()

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

    def test_every_new_ko_decision_moves_act_to_to_analysis(self):
        for decision in Act.KoDecision.new_values():
            with self.subTest(decision=decision):
                act = self._create_act(self.status_ko)

                apply_ko_decision(act, self.ko_user, [(None, decision, 'Решение КО')])

                act.refresh_from_db()
                self.assertEqual(act.status.code, 'TO_ANALYSIS')
                self.assertEqual(act.ko_decision_by, self.ko_user)
                self.assertIsNotNone(act.ko_decision_at)

    def test_legacy_ko_decision_cannot_be_used_for_a_new_transition(self):
        act = self._create_act(self.status_ko)

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(act, self.ko_user, [(None, Act.KoDecision.RETURN, 'Старое решение')])

    def test_every_defect_requires_a_ko_decision_before_transition_to_to(self):
        act = self._create_act(self.status_ko)
        first_defect = ActDefect.objects.create(
            act=act,
            defect_type=self.defect_type,
            description='Первый дефект',
            detected_at='2026-07-21',
        )
        second_defect = ActDefect.objects.create(
            act=act,
            defect_type=self.defect_type,
            description='Второй дефект',
            detected_at='2026-07-21',
        )

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(
                act,
                self.ko_user,
                [(first_defect, Act.KoDecision.ALLOW_NO_REWORK, 'Решение')],
            )

        apply_ko_decision(
            act,
            self.ko_user,
            [
                (first_defect, Act.KoDecision.ALLOW_NO_REWORK, 'Решение по первому'),
                (second_defect, Act.KoDecision.PROHIBIT_USE, 'Решение по второму'),
            ],
        )
        act.refresh_from_db()
        first_defect.refresh_from_db()
        second_defect.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertEqual(first_defect.ko_decision, Act.KoDecision.ALLOW_NO_REWORK)
        self.assertEqual(second_defect.ko_decision, Act.KoDecision.PROHIBIT_USE)

    def test_to_analysis_moves_act_to_actions_assigned(self):
        act = self._create_act(self.status_to)

        apply_to_analysis(act, self.to_user, 'Корневая причина', 'Мероприятия')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertEqual(act.to_analysis_by, self.to_user)
        self.assertIsNotNone(act.to_analysis_at)

    def _structured_analysis_post(self, **overrides):
        data = {
            'root-TOTAL_FORMS': '1',
            'root-0-root_cause': 'Корневая причина',
            'root-0-actions-TOTAL_FORMS': '1',
            'root-0-actions-0-comment': 'Корректирующее мероприятие',
            'root-0-actions-0-department': str(self.department.pk),
            'root-0-actions-0-responsible': str(self.to_user.pk),
            'root-0-actions-0-due_date': timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return data

    def test_structured_analysis_requires_minimum_structure(self):
        form = ToAnalysisStructureForm({'root-TOTAL_FORMS': '0'})

        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors)

    def test_structured_analysis_validates_department_user_and_due_date(self):
        form = ToAnalysisStructureForm(
            self._structured_analysis_post(
                **{
                    'root-0-actions-0-department': str(self.other_department.pk),
                    'root-0-actions-0-due_date': (timezone.localdate() - timedelta(days=1)).isoformat(),
                }
            )
        )

        self.assertFalse(form.is_valid())
        errors = form.root_rows[0]['actions'][0]['errors']
        self.assertIn('responsible', errors)
        self.assertIn('due_date', errors)

    def test_structured_analysis_saves_all_data_and_transitions_atomically(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())

        apply_structured_to_analysis(act, self.to_user, form.analysis_data)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertEqual(act.to_root_cause, 'Корневая причина')
        self.assertEqual(act.to_action_summary, 'Корректирующее мероприятие')
        self.assertEqual(ActRootAnalysis.objects.filter(act=act).count(), 1)
        self.assertEqual(ActCorrectiveAction.objects.filter(root_analysis__act=act).count(), 1)

    def test_structured_analysis_wrong_role_does_not_save_partial_data(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())

        with self.assertRaises(ActWorkflowError):
            apply_structured_to_analysis(act, self.ko_user, form.analysis_data)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertFalse(ActRootAnalysis.objects.filter(act=act).exists())

    def test_return_to_otk_requires_comment_without_changing_act(self):
        act = self._create_act(self.status_ko)

        with self.assertRaises(ActWorkflowError):
            return_to_otk(act, self.ko_user, '   ')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertFalse(ActComment.objects.filter(act=act).exists())

    def test_return_to_otk_saves_comment_and_history_with_transition(self):
        act = self._create_act(self.status_ko)

        return_to_otk(act, self.ko_user, 'Уточнить номер партии.')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(ActComment.objects.get(act=act).text, 'Уточнить номер партии.')
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act,
                event_type=ActHistoryEvent.EventType.COMMENT_ADDED,
            ).count(),
            1,
        )

    def test_return_to_ko_requires_comment_without_changing_act(self):
        act = self._create_act(self.status_to)

        with self.assertRaises(ActWorkflowError):
            return_to_ko(act, self.to_user, '  ')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertFalse(ActComment.objects.filter(act=act).exists())

    def test_return_to_ko_saves_comment_and_history_atomically(self):
        act = self._create_act(self.status_to)

        return_to_ko(act, self.to_user, 'Уточнить решение КО.')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(ActComment.objects.get(act=act).text, 'Уточнить решение КО.')
        self.assertEqual(
            ActHistoryEvent.objects.filter(act=act, event_type=ActHistoryEvent.EventType.COMMENT_ADDED).count(),
            1,
        )
        self.assertEqual(
            ActHistoryEvent.objects.filter(act=act, event_type=ActHistoryEvent.EventType.RETURNED_TO_KO).count(),
            1,
        )

    def test_return_to_to_preserves_structured_analysis(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        root_analysis_id = ActRootAnalysis.objects.get(act=act).pk

        return_to_to(act, self.otk_user, 'Уточнить срок мероприятия.')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'TO_ANALYSIS')
        self.assertTrue(ActRootAnalysis.objects.filter(pk=root_analysis_id).exists())
        self.assertEqual(ActCorrectiveAction.objects.filter(root_analysis_id=root_analysis_id).count(), 1)
        prefilled_form = ToAnalysisStructureForm(
            root_analyses=ActRootAnalysis.objects.filter(act=act).prefetch_related('corrective_actions')
        )
        self.assertEqual(prefilled_form.root_rows[0]['root_cause'], 'Корневая причина')
        self.assertEqual(prefilled_form.root_rows[0]['actions'][0]['comment'], 'Корректирующее мероприятие')
        self.assertEqual(
            ActHistoryEvent.objects.filter(act=act, event_type=ActHistoryEvent.EventType.RETURNED_TO_TO).count(),
            1,
        )

    def test_approve_archives_act_and_records_approver(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)

        approve_act(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ARCHIVED')
        self.assertEqual(act.approved_by, self.otk_user)
        self.assertIsNotNone(act.approved_at)
        self.assertEqual(Task.objects.filter(act=act).count(), 1)
        self.assertEqual(Task.objects.get(act=act).status.code, 'NEW')
        self.assertEqual(
            ActHistoryEvent.objects.filter(act=act, event_type=ActHistoryEvent.EventType.APPROVED).count(),
            1,
        )

    def test_approval_rolls_back_when_corrective_action_is_invalid(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        action.responsible = self.other_user
        action.save(update_fields=['responsible'])

        with self.assertRaises(ActWorkflowError):
            approve_act(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertFalse(Task.objects.filter(act=act).exists())

    def test_approval_does_not_create_duplicate_tasks(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        approve_act(act, self.otk_user)

        with self.assertRaises(ActWorkflowError):
            approve_act(act, self.otk_user)

        self.assertEqual(Task.objects.filter(act=act).count(), 1)

    def test_approval_rejects_due_date_before_approval_date(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        action.due_date = timezone.localdate() - timedelta(days=1)
        action.save(update_fields=['due_date'])

        with self.assertRaises(ActWorkflowError):
            approve_act(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertFalse(Task.objects.filter(act=act).exists())
    def test_wrong_roles_raise_workflow_error(self):
        ko_act = self._create_act(self.status_ko)
        to_act = self._create_act(self.status_to)
        created_act = self._create_act(self.status_created)

        with self.assertRaises(ActWorkflowError):
            apply_ko_decision(ko_act, self.otk_user, [(None, Act.KoDecision.ALLOW_NO_REWORK, '')])
        with self.assertRaises(ActWorkflowError):
            apply_to_analysis(to_act, self.ko_user, 'Причина', 'Мероприятия')
        with self.assertRaises(ActWorkflowError):
            return_to_ko(to_act, self.ko_user, 'Вернуть КО')
        with self.assertRaises(ActWorkflowError):
            send_to_ko(created_act, self.to_user)
