from datetime import date, timedelta
from importlib import import_module

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.forms import ToAnalysisStructureForm
from acts.models import (
    Act, ActComment, ActCorrectiveAction, ActDefect, ActHistoryEvent, ActRootAnalysis,
    calculate_act_due_date,
)
from acts.permissions import (
    can_approve_act,
    can_contribute_to_act,
    can_edit_act,
    can_send_to_ko,
    get_visible_acts_queryset,
)
from acts.selectors import get_related_tasks
from acts.services import ActWorkflowError, apply_ko_decision, apply_structured_to_analysis, apply_to_analysis, approve_act, return_to_ko, return_to_otk, return_to_to, send_to_ko
from references.models import ActStatus, DefectType, Operation
from tasks.models import Task
from tasks.permissions import can_complete_task
from tasks.services import ensure_act_rejection_task


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
            nomenclature='Катушка',
            status=status,
        )

    def _mp_defect(self, act, *, znp, party, rejected):
        return ActDefect.objects.create(
            act=act,
            defect_type=self.defect_type,
            workshop=ActDefect.Workshop.MP_SHOP,
            operation=self.operation,
            mp_type=ActDefect.MpType.OL,
            znp_number=znp,
            party_number=party,
            description='Дефект МП',
            detected_at='2026-09-01',
            checked_quantity=rejected + 10,
            nonconforming_quantity=rejected,
        )

    @staticmethod
    def _act_tasks(act):
        """The act's corrective-action tasks only.

        An act also owns `ACT_WORKFLOW` routing entries now — one per stage it
        waits on — and they are not what approval creates.
        """
        return Task.objects.filter(act=act, source_type=Task.SourceType.ACT)

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
            'root-0-actions-0-assignees': [str(self.to_user.pk)],
            'root-0-actions-0-due_date': timezone.localdate().isoformat(),
        }
        data.update(overrides)
        return data

    def test_structured_analysis_requires_minimum_structure(self):
        form = ToAnalysisStructureForm({'root-TOTAL_FORMS': '0'})

        self.assertFalse(form.is_valid())
        self.assertTrue(form.non_field_errors)

    def test_structured_analysis_requires_assignee_from_selected_department_and_validates_due_date(self):
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
        self.assertIn('due_date', errors)
        self.assertIn('assignees', errors)

    def test_structured_analysis_requires_unique_active_department_assignees(self):
        form = ToAnalysisStructureForm(self._structured_analysis_post(
            **{'root-0-actions-0-assignees': [str(self.to_user.pk), str(self.to_user.pk)]}
        ))
        self.assertFalse(form.is_valid())
        self.assertIn('assignees', form.root_rows[0]['actions'][0]['errors'])

    def test_structured_analysis_allows_the_same_assignee_in_different_actions(self):
        data = self._structured_analysis_post()
        data.update({
            'root-0-actions-TOTAL_FORMS': '2',
            'root-0-actions-1-comment': 'Второе мероприятие',
            'root-0-actions-1-department': str(self.department.pk),
            'root-0-actions-1-assignees': [str(self.to_user.pk)],
            'root-0-actions-1-due_date': timezone.localdate().isoformat(),
        })

        form = ToAnalysisStructureForm(data)

        self.assertTrue(form.is_valid())

    def test_structured_analysis_allows_and_restores_cross_department_assignees(self):
        form = ToAnalysisStructureForm(self._structured_analysis_post(
            **{
                'root-0-actions-0-assignees': [str(self.to_user.pk), str(self.other_user.pk)],
                'root-0-actions-0-assignee_departments': [str(self.other_department.pk)],
            }
        ))
        self.assertTrue(form.is_valid())
        act = self._create_act(self.status_to)
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        return_to_to(act, self.otk_user, 'Нужна доработка.')
        prefilled_form = ToAnalysisStructureForm(
            root_analyses=ActRootAnalysis.objects.filter(act=act).prefetch_related('corrective_actions__assignees')
        )
        self.assertEqual(
            prefilled_form.root_rows[0]['actions'][0]['assignees'],
            [str(self.to_user.pk), str(self.other_user.pk)],
        )

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
        self.assertEqual(self._act_tasks(act).count(), 1)
        self.assertEqual(self._act_tasks(act).get().status.code, 'IN_PROGRESS')
        self.assertEqual(
            ActHistoryEvent.objects.filter(act=act, event_type=ActHistoryEvent.EventType.APPROVED).count(),
            1,
        )

    def test_approval_creates_a_task_that_is_explicitly_act_sourced(self):
        # The act path is the only writer of `ACT` tasks and must say so
        # itself: an act task that fell back on the field default, or that
        # carried a protocol relation, is what the source constraint exists to
        # stop, and the registry reads `source_type` rather than the relations.
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)

        approve_act(act, self.otk_user)

        task = self._act_tasks(act).get()
        self.assertEqual(task.source_type, Task.SourceType.ACT)
        self.assertIsNotNone(task.source_action_id)
        self.assertIsNotNone(task.root_analysis_id)
        self.assertIsNone(task.protocol_id)
        self.assertIsNone(task.protocol_action_id)

    def test_approval_creates_one_shared_task_for_all_action_assignees(self):
        second_to_user = self._create_user('to_second', UserProfile.Role.TO)
        second_to_user.userprofile.department = self.department
        second_to_user.userprofile.save()
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post(
            **{
                'root-0-actions-0-assignees': [str(self.to_user.pk), str(second_to_user.pk)],
                'root-0-actions-0-assignee_departments': [str(self.department.pk)],
            }
        ))
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)

        approve_act(act, self.otk_user)

        task = self._act_tasks(act).get()
        self.assertEqual(self._act_tasks(act).count(), 1)
        self.assertSetEqual(set(task.assignees.values_list('user_id', flat=True)), {self.to_user.pk, second_to_user.pk})

    def test_approval_rolls_back_when_corrective_action_is_invalid(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        self.to_user.is_active = False
        self.to_user.save(update_fields=['is_active'])

        with self.assertRaises(ActWorkflowError):
            approve_act(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'OTK_REVIEW')
        self.assertFalse(self._act_tasks(act).exists())

    def test_approval_does_not_create_duplicate_tasks(self):
        act = self._create_act(self.status_to)
        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        approve_act(act, self.otk_user)

        with self.assertRaises(ActWorkflowError):
            approve_act(act, self.otk_user)

        self.assertEqual(self._act_tasks(act).count(), 1)

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
        self.assertFalse(self._act_tasks(act).exists())
    def test_route_deadline_and_workflow_tasks_follow_the_act_not_its_author(self):
        """The three facts the new act workflow rests on, in one pass.

        The deadline is three Monday–Friday days from *creation*; each stage of
        the route owns exactly one routing task, closed as the act moves; and
        the final ОТК review belongs to the department, not to the person who
        created the act.
        """
        # 1. Three working days from the creation date, weekends stepped over.
        for created_on, expected in (
            (date(2026, 8, 31), date(2026, 9, 3)),   # Monday    → Thursday
            (date(2026, 9, 3), date(2026, 9, 8)),    # Thursday  → Tuesday
            (date(2026, 9, 4), date(2026, 9, 9)),    # Friday    → Wednesday
        ):
            self.assertEqual(calculate_act_due_date(created_on), expected)

        second_otk = self._create_user('otk_second', UserProfile.Role.OTK)
        act = self._create_act(self.status_created)
        act.due_date = calculate_act_due_date()
        act.save(update_fields=['due_date'])

        def stage_task():
            return Task.objects.filter(
                act=act,
                source_type=Task.SourceType.ACT_WORKFLOW,
            ).exclude(status__code='COMPLETED').get()

        # 2. Creation owns no routing task — the creator already has the act.
        self.assertFalse(
            Task.objects.filter(act=act, source_type=Task.SourceType.ACT_WORKFLOW).exists()
        )

        send_to_ko(act, self.otk_user)
        ko_task = stage_task()
        self.assertEqual(ko_task.workflow_stage, Task.WorkflowStage.KO_REVIEW)
        self.assertEqual(
            set(ko_task.assignees.values_list('user_id', flat=True)), {self.ko_user.pk}
        )
        self.assertEqual(ko_task.due_date, act.due_date)
        # A routing task is never finished with an execution comment.
        self.assertFalse(can_complete_task(ko_task, self.ko_user))

        act = apply_ko_decision(
            act, self.ko_user, [(None, Act.KoDecision.PROHIBIT_USE, 'Решение')]
        )
        ko_task.refresh_from_db()
        self.assertEqual(ko_task.status.code, 'COMPLETED')
        to_task = stage_task()
        self.assertEqual(to_task.workflow_stage, Task.WorkflowStage.TO_ANALYSIS)
        self.assertEqual(
            set(to_task.assignees.values_list('user_id', flat=True)),
            {self.to_user.pk, self.other_user.pk},
        )

        form = ToAnalysisStructureForm(self._structured_analysis_post())
        self.assertTrue(form.is_valid())
        act = apply_structured_to_analysis(act, self.to_user, form.analysis_data)
        to_task.refresh_from_db()
        self.assertEqual(to_task.status.code, 'COMPLETED')
        otk_task = stage_task()
        self.assertEqual(otk_task.workflow_stage, Task.WorkflowStage.OTK_REVIEW)
        self.assertEqual(
            set(otk_task.assignees.values_list('user_id', flat=True)),
            {self.otk_user.pk, second_otk.pk},
        )

        # 3. Any active ОТК employee closes the review, not only the author.
        self.assertTrue(can_approve_act(act, second_otk))
        approve_act(act, second_otk)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ARCHIVED')
        self.assertEqual(act.approved_by, second_otk)
        otk_task.refresh_from_db()
        self.assertEqual(otk_task.status.code, 'COMPLETED')
        # The route ended: no new routing task was opened.
        self.assertFalse(
            Task.objects.filter(act=act, source_type=Task.SourceType.ACT_WORKFLOW)
            .exclude(status__code='COMPLETED')
            .exists()
        )
        # And the corrective-action tasks are untouched by any of it.
        self.assertEqual(self._act_tasks(act).count(), 1)

    def test_backfill_repairs_active_acts_and_orphaned_created_otk_stays_workable(self):
        """The two things an existing production database needs on upgrade.

        Acts already in flight get the new deadline and the routing task their
        stage implies; an act whose author is no longer an eligible ОТК
        employee stops being unreachable, while an act with a live author keeps
        belonging to them alone.
        """
        backfill = import_module(
            'tasks.migrations.0013_backfill_active_act_deadlines_and_workflow_tasks'
        )
        second_otk = self._create_user('otk_backfill', UserProfile.Role.OTK)

        created = self._create_act(self.status_created)
        ko_act = self._create_act(self.status_ko)
        archived = self._create_act(self.status_archived)
        archived.due_date = date(2020, 1, 1)
        archived.save(update_fields=['due_date'])
        # An act whose stage is already represented must not gain a second one.
        represented = self._create_act(self.status_created)
        send_to_ko(represented, self.otk_user)
        represented.refresh_from_db()
        existing_task = Task.objects.get(
            act=represented, source_type=Task.SourceType.ACT_WORKFLOW
        )

        backfill.backfill_active_acts(django_apps, None)

        for act in (created, ko_act, represented):
            act.refresh_from_db()
            self.assertEqual(
                act.due_date,
                calculate_act_due_date(timezone.localtime(act.created_at).date()),
            )
        # Archived history is never rewritten.
        archived.refresh_from_db()
        self.assertEqual(archived.due_date, date(2020, 1, 1))

        ko_task = Task.objects.get(act=ko_act, source_type=Task.SourceType.ACT_WORKFLOW)
        self.assertEqual(ko_task.workflow_stage, Task.WorkflowStage.KO_REVIEW)
        self.assertEqual(
            set(ko_task.assignees.values_list('user_id', flat=True)), {self.ko_user.pk}
        )
        self.assertEqual(ko_task.due_date, ko_act.due_date)
        # No routing task for `CREATED_OTK`, and no duplicate where one existed.
        self.assertFalse(
            Task.objects.filter(
                act=created, source_type=Task.SourceType.ACT_WORKFLOW
            ).exists()
        )
        self.assertEqual(
            list(
                Task.objects.filter(
                    act=represented, source_type=Task.SourceType.ACT_WORKFLOW
                ).values_list('pk', flat=True)
            ),
            [existing_task.pk],
        )
        # Idempotent: a second run adds nothing.
        backfill.backfill_active_acts(django_apps, None)
        self.assertEqual(
            Task.objects.filter(
                act=ko_act, source_type=Task.SourceType.ACT_WORKFLOW
            ).count(),
            1,
        )

        # An act returned to `CREATED_OTK` belongs to its author while the
        # author is still an eligible ОТК employee.
        orphan = self._create_act(self.status_created)
        self.assertFalse(can_edit_act(orphan, second_otk))
        self.assertFalse(can_send_to_ko(orphan, second_otk))
        self.assertNotIn(orphan, get_visible_acts_queryset(second_otk))

        # Once the author is not, any active ОТК employee may pick it up.
        self.otk_user.userprofile.is_active = False
        self.otk_user.userprofile.save(update_fields=['is_active'])
        self.assertTrue(can_edit_act(orphan, second_otk))
        self.assertTrue(can_send_to_ko(orphan, second_otk))
        self.assertTrue(can_contribute_to_act(orphan, second_otk))
        self.assertIn(orphan, get_visible_acts_queryset(second_otk))

    def test_prohibited_mp_defects_create_one_pdo_task_for_the_whole_act(self):
        """The ПДО replanning notice: what triggers it, and what it says."""
        # Seeded by `accounts.0003`, so it is looked up rather than created.
        pdo_department = Department.objects.get(code='PDO')
        # Department, not role: a Руководитель filed under ПДО plans products
        # too, and a ПДО-role user in another department does not.
        planner = self._create_user('pdo_manager', UserProfile.Role.MANAGER)
        planner.userprofile.department = pdo_department
        planner.userprofile.save(update_fields=['department'])
        outsider = self._create_user('pdo_role_elsewhere', UserProfile.Role.PDO)
        outsider.userprofile.department = self.other_department
        outsider.userprofile.save(update_fields=['department'])

        act = self._create_act(self.status_ko)
        act.nomenclature = 'МП 120/200-40'
        act.order_number = '12345'
        act.due_date = date(2026, 9, 10)
        act.save(update_fields=['nomenclature', 'order_number', 'due_date'])
        first = self._mp_defect(act, znp='6789', party='15', rejected=8)
        second = self._mp_defect(act, znp='7000', party='16', rejected=2)
        # A ПиР defect is never planned for replacement, whatever КО decided.
        pir = ActDefect.objects.create(
            act=act, defect_type=self.defect_type, workshop=ActDefect.Workshop.PIR_SHOP,
            znp_number='9001', detected_at='2026-09-01',
            checked_quantity=5, nonconforming_quantity=1,
        )

        apply_ko_decision(act, self.ko_user, [
            (first, Act.KoDecision.PROHIBIT_USE, 'Брак'),
            (second, Act.KoDecision.PROHIBIT_USE, 'Брак'),
            (pir, Act.KoDecision.PROHIBIT_USE, 'Брак'),
        ])

        task = Task.objects.get(act=act, source_type=Task.SourceType.ACT_REJECTION)
        # One task for the act, one line per qualifying defect, in act order,
        # with no synthetic total across ЗНП rows.
        self.assertEqual(
            task.task_text.splitlines(),
            [
                'МП 120/200-40 забраковано 8 шт. по заказу №12345, ЗНП №6789, Партия №15.',
                'МП 120/200-40 забраковано 2 шт. по заказу №12345, ЗНП №7000, Партия №16.',
            ],
        )
        self.assertEqual(
            set(task.assignees.values_list('user_id', flat=True)), {planner.pk}
        )
        self.assertEqual(task.department_id, pdo_department.pk)
        self.assertEqual(task.due_date, act.due_date)
        self.assertEqual(task.created_by, self.ko_user)
        # Ordinary executable work: completable, and not in «Связанные мероприятия».
        self.assertTrue(can_complete_task(task, planner))
        self.assertNotIn(task, get_related_tasks(act, planner))
        # The routing queue moved on independently.
        self.assertEqual(
            Task.objects.filter(act=act, source_type=Task.SourceType.ACT_WORKFLOW)
            .exclude(status__code='COMPLETED')
            .get()
            .workflow_stage,
            Task.WorkflowStage.TO_ANALYSIS,
        )

        # A repeat creates nothing more.
        ensure_act_rejection_task(act, [first, second], created_by=self.ko_user)
        self.assertEqual(
            Task.objects.filter(act=act, source_type=Task.SourceType.ACT_REJECTION).count(), 1
        )

        # A permitting decision on МП products produces no notice at all.
        allowed_act = self._create_act(self.status_ko)
        allowed_defect = self._mp_defect(allowed_act, znp='1', party='2', rejected=1)
        apply_ko_decision(
            allowed_act, self.ko_user,
            [(allowed_defect, Act.KoDecision.ALLOW_NO_REWORK, 'Разрешено')],
        )
        self.assertFalse(
            Task.objects.filter(
                act=allowed_act, source_type=Task.SourceType.ACT_REJECTION
            ).exists()
        )

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
