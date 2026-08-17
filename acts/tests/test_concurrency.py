"""Real concurrency tests, executed only on a backend with row-level locking.

Every test here runs two or more threads against the *same* committed rows, so
they use `TransactionTestCase` (each thread needs its own committed view of the
data, which `TestCase`'s single wrapping transaction cannot provide).

They are skipped on SQLite: `select_for_update()` is a documented no-op there,
so the outcome would prove nothing. On PostgreSQL they are required and must
never be disabled to make CI pass.
"""

import threading
from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection, connections, transaction
from django.test import SimpleTestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from accounts.models import Department, UserProfile
from acts.models import (
    Act,
    ActCorrectiveAction,
    ActCorrectiveActionAssignee,
    ActHistoryEvent,
    ActRootAnalysis,
)
from acts.services import ActWorkflowError, approve_act, send_to_ko
from references.models import ActStatus, DefectType, Operation, TaskStatus
from tasks.models import Task, TaskAssignee
from tasks.services import TaskWorkflowError, complete_task

# Every worker joins with a finite timeout so a lost wake-up fails the test
# instead of hanging a GitHub Actions job forever.
BARRIER_TIMEOUT = 20
JOIN_TIMEOUT = 60


def run_in_parallel(targets):
    """Run callables in separate threads, released together, and collect results.

    Each thread owns its own database connection (Django opens one lazily per
    thread) and closes it in a `finally`, so no connection leaks into the next
    test.
    """
    barrier = threading.Barrier(len(targets), timeout=BARRIER_TIMEOUT)
    results = [None] * len(targets)

    def worker(index, target):
        try:
            barrier.wait()
            try:
                results[index] = ('ok', target())
            except Exception as exc:  # noqa: BLE001 - recorded and asserted on
                results[index] = ('error', exc)
        finally:
            connections.close_all()

    threads = [
        threading.Thread(target=worker, args=(index, target), daemon=True)
        for index, target in enumerate(targets)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=JOIN_TIMEOUT)
    for thread in threads:
        assert not thread.is_alive(), 'A concurrent worker did not finish before the timeout.'
    return results


def outcomes(results):
    return [kind for kind, _payload in results]


@skipUnlessDBFeature('has_select_for_update')
class ActConcurrencyTests(TransactionTestCase):
    """Two simultaneous requests must produce exactly one applied transition."""

    reset_sequences = True

    def setUp(self):
        self.status_created, _ = ActStatus.objects.get_or_create(
            code='CREATED_OTK', defaults={'name': 'Создан ОТК'}
        )
        self.status_ko, _ = ActStatus.objects.get_or_create(
            code='KO_REVIEW', defaults={'name': 'На рассмотрении КО'}
        )
        self.status_otk_review, _ = ActStatus.objects.get_or_create(
            code='OTK_REVIEW', defaults={'name': 'Проверка ОТК'}
        )
        self.status_archived, _ = ActStatus.objects.get_or_create(
            code='ARCHIVED', defaults={'name': 'Архивирован'}
        )
        self.task_status, _ = TaskStatus.objects.get_or_create(
            code='IN_PROGRESS', defaults={'name': 'В работе'}
        )
        TaskStatus.objects.get_or_create(code='COMPLETED', defaults={'name': 'Выполнено'})
        self.operation = Operation.objects.create(code='CONC_OP', name='Операция')
        self.defect_type = DefectType.objects.create(code='CONC_DEFECT', name='Дефект')
        self.department = Department.objects.create(code='CONC_TO', name='Технологический отдел')

        self.otk_user = self._create_user('conc_otk', UserProfile.Role.OTK)
        self.to_user = self._create_user('conc_to', UserProfile.Role.TO)
        self.to_user.userprofile.department = self.department
        self.to_user.userprofile.save(update_fields=['department'])

    def tearDown(self):
        connections.close_all()

    def _create_user(self, username, role):
        user = User.objects.create_user(username=username, password='demo12345')
        user.userprofile.role = role
        user.userprofile.save(update_fields=['role'])
        return user

    def _create_act(self, status, **overrides):
        values = {
            'created_by': self.otk_user,
            'nomenclature': 'Катушка',
            'status': status,
        }
        values.update(overrides)
        return Act.objects.create(**values)

    def test_two_simultaneous_send_to_ko_apply_the_transition_once(self):
        act = self._create_act(self.status_created)

        def attempt():
            return send_to_ko(Act.objects.select_related('status').get(pk=act.pk), self.otk_user)

        results = run_in_parallel([attempt, attempt])

        self.assertEqual(sorted(outcomes(results)), ['error', 'ok'])
        failure = next(payload for kind, payload in results if kind == 'error')
        self.assertIsInstance(failure, ActWorkflowError)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.SENT_TO_KO
            ).count(),
            1,
        )

    def test_two_simultaneous_approvals_create_no_duplicate_tasks(self):
        act = self._create_act(self.status_otk_review, to_analysis_by=self.to_user)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
        )
        ActCorrectiveActionAssignee.objects.create(corrective_action=action, user=self.to_user)

        def attempt():
            return approve_act(Act.objects.select_related('status').get(pk=act.pk), self.otk_user)

        results = run_in_parallel([attempt, attempt])

        self.assertEqual(sorted(outcomes(results)), ['error', 'ok'])
        failure = next(payload for kind, payload in results if kind == 'error')
        self.assertIsInstance(failure, ActWorkflowError)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ARCHIVED')
        self.assertEqual(Task.objects.filter(act=act).count(), 1)
        self.assertEqual(TaskAssignee.objects.filter(task__act=act).count(), 1)
        self.assertEqual(
            ActHistoryEvent.objects.filter(
                act=act, event_type=ActHistoryEvent.EventType.APPROVED
            ).count(),
            1,
        )

    def test_two_simultaneous_completions_finish_one_task_once(self):
        act = self._create_act(self.status_archived, to_analysis_by=self.to_user)
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment='Мероприятие',
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
        )
        task = Task.objects.create(
            source_action=action,
            act=act,
            root_analysis=root,
            task_text=action.comment,
            department=self.department,
            due_date=action.due_date,
            created_by=self.otk_user,
            status=self.task_status,
        )
        TaskAssignee.objects.create(task=task, user=self.to_user)

        def attempt():
            return complete_task(Task.objects.get(pk=task.pk), self.to_user, 'Выполнено.')

        results = run_in_parallel([attempt, attempt])

        self.assertEqual(sorted(outcomes(results)), ['error', 'ok'])
        failure = next(payload for kind, payload in results if kind == 'error')
        self.assertIsInstance(failure, TaskWorkflowError)

        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.completed_by, self.to_user)


class ConcurrencyBackendGuardTests(SimpleTestCase):
    """Makes the skip decision visible instead of silently passing."""

    def test_row_locking_support_matches_the_active_backend(self):
        if connection.vendor == 'postgresql':
            self.assertTrue(
                connection.features.has_select_for_update,
                'PostgreSQL must report row locking support; concurrency tests rely on it.',
            )
        else:
            self.assertEqual(connection.vendor, 'sqlite')
