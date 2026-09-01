"""«Разбить задачу для исполнителей»: execution only, one corrective action.

The shared mode is already covered end to end by the existing act workflow
tests; what is exercised here is only what the flag adds — where it may be
stored, how many tasks approval then creates, and that those tasks are
genuinely independent of one another and cannot be created twice.
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import Act, ActCorrectiveAction, ActRootAnalysis
from acts.services import apply_structured_to_analysis, approve_act
from references.models import ActStatus
from tasks.models import Task
from tasks.services import complete_task, create_act_action_task


class ActTaskSplitTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.status_to = ActStatus.objects.create(code='TO_ANALYSIS', name='На анализе ТО')
        cls.status_otk_review = ActStatus.objects.get(code='OTK_REVIEW')
        cls.department = Department.objects.create(code='TO', name='Технологический отдел')
        cls.otk_user = cls._user('split_otk', UserProfile.Role.OTK, None)
        cls.to_user = cls._user('split_to', UserProfile.Role.TO, cls.department)
        cls.other_user = cls._user('split_other', UserProfile.Role.TO, cls.department)

    @classmethod
    def _user(cls, username, role, department):
        user = User.objects.create_user(username=username, password='demo12345')
        profile = user.userprofile
        profile.role = role
        profile.department = department
        profile.save()
        return user

    def _create_act(self, status):
        return Act.objects.create(
            created_by=self.otk_user, nomenclature='Катушка', status=status,
        )

    def _analysis_payload(self, *, split, assignees):
        return [
            {
                'root_cause': 'Плохое оснащение участка',
                'actions': [
                    {
                        'comment': 'Заменить оснастку',
                        'department': self.department,
                        'assignees': list(assignees),
                        'due_date': timezone.localdate() + timedelta(days=7),
                        'split_for_assignees': split,
                    }
                ],
            }
        ]

    def _act_at_otk_review(self, *, split, assignees):
        """An act whose single corrective action names `assignees`, ready to approve."""
        act = self._create_act(self.status_to)
        apply_structured_to_analysis(
            act, self.to_user, self._analysis_payload(split=split, assignees=assignees)
        )
        act.refresh_from_db()
        return act

    def test_the_analysis_stores_the_flag_only_where_splitting_can_mean_anything(self):
        """Two executors keep the choice; one normalizes it away on the server.

        The browser disables the checkbox below two исполнителя, but that is
        presentation: this stores it enabled in both cases, the way a stale
        page or a hand-made request would, and
        `apply_structured_to_analysis()` is what decides.
        """
        act = self._create_act(self.status_to)
        payload = self._analysis_payload(
            split=True, assignees=[self.to_user, self.other_user]
        )
        payload[0]['actions'].append(
            {
                'comment': 'Обновить инструкцию',
                'department': self.department,
                'assignees': [self.to_user],
                'due_date': timezone.localdate() + timedelta(days=7),
                'split_for_assignees': True,
            }
        )

        apply_structured_to_analysis(act, self.to_user, payload)

        multi, single = list(
            ActCorrectiveAction.objects.filter(root_analysis__act=act).order_by('display_order')
        )
        self.assertTrue(multi.split_for_assignees)
        # One executor: splitting the work between them alone means nothing.
        self.assertFalse(single.split_for_assignees)
        # The act's own structure is untouched by the flag.
        self.assertEqual(ActRootAnalysis.objects.filter(act=act).count(), 1)

    def test_approval_splits_a_marked_action_into_one_task_per_assignee(self):
        assignees = [self.to_user, self.other_user]
        act = self._act_at_otk_review(split=True, assignees=assignees)

        approve_act(act, self.otk_user)
        act.refresh_from_db()

        self.assertEqual(act.status.code, 'ARCHIVED')
        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        tasks = list(
            Task.objects.filter(act=act, source_type=Task.SourceType.ACT)
            .order_by('individual_assignee_id')
            .prefetch_related('assignees')
        )
        self.assertEqual(len(tasks), 2)
        for task, user in zip(tasks, sorted(assignees, key=lambda item: item.pk)):
            # Same corrective action, same act, root analysis, wording,
            # department and deadline…
            self.assertEqual(task.source_type, Task.SourceType.ACT)
            self.assertEqual(task.source_action_id, action.pk)
            self.assertEqual(task.root_analysis_id, action.root_analysis_id)
            self.assertEqual(task.task_text, action.comment)
            self.assertEqual(task.department_id, action.department_id)
            self.assertEqual(task.due_date, action.due_date)
            # …and exactly one assignee, who is the person it names.
            self.assertEqual(task.individual_assignee_id, user.pk)
            self.assertEqual([a.user_id for a in task.assignees.all()], [user.pk])

        # One corrective action, one row — only its execution was split.
        self.assertEqual(ActCorrectiveAction.objects.filter(root_analysis__act=act).count(), 1)
        # And the read-only «Анализ ТО» cell reports progress, not two rows.
        self.assertEqual(action.task_state_label, '0 из 2 выполнено')

        # The shared mode is unchanged beside it: one task, every assignee on it.
        shared_act = self._act_at_otk_review(split=False, assignees=assignees)
        approve_act(shared_act, self.otk_user)
        shared_task = Task.objects.get(act=shared_act, source_type=Task.SourceType.ACT)
        self.assertIsNone(shared_task.individual_assignee_id)
        self.assertEqual(
            sorted(a.user_id for a in shared_task.assignees.all()),
            sorted(user.pk for user in assignees),
        )
        self.assertEqual(
            ActCorrectiveAction.objects.get(root_analysis__act=shared_act).task_state_label,
            str(shared_task.status),
        )

    def test_a_split_task_finishes_alone_and_can_never_be_created_twice(self):
        assignees = [self.to_user, self.other_user]
        act = self._act_at_otk_review(split=True, assignees=assignees)
        approve_act(act, self.otk_user)
        action = ActCorrectiveAction.objects.get(root_analysis__act=act)
        tasks = {
            task.individual_assignee_id: task
            for task in Task.objects.filter(act=act, source_type=Task.SourceType.ACT)
        }

        completed = complete_task(tasks[self.to_user.pk], self.to_user, 'Оснастка заменена.')

        self.assertEqual(completed.status.code, 'COMPLETED')
        sibling = tasks[self.other_user.pk]
        sibling.refresh_from_db()
        # Nobody else's work was closed by someone else finishing theirs.
        self.assertEqual(sibling.status.code, 'IN_PROGRESS')
        self.assertIsNone(sibling.completed_by_id)
        self.assertEqual(action.task_state_label, '1 из 2 выполнено')

        # A repeated or faulty generation cannot hand the same person a second
        # copy, nor add a shared task beside the split ones. The database says
        # so, not a service check a concurrent caller could race past.
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_act_action_task(
                action,
                [self.to_user.pk],
                created_by=self.otk_user,
                individual_assignee_id=self.to_user.pk,
            )
        shared_act = self._act_at_otk_review(split=False, assignees=assignees)
        approve_act(shared_act, self.otk_user)
        shared_action = ActCorrectiveAction.objects.get(root_analysis__act=shared_act)
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_act_action_task(
                shared_action,
                [user.pk for user in assignees],
                created_by=self.otk_user,
            )
