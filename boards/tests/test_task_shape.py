"""The `BOARD` source shape on `tasks.Task`: `clean()` and the check constraint."""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from references.models import TaskStatus
from tasks.models import Task

from .helpers import BoardFixtureMixin, due


# Relations a `BOARD` task must not carry. The ids are never dereferenced:
# `clean()` reads `<name>_id`, and the check constraint refuses the row before
# any (deferred) foreign key is checked, so nothing dangling is ever stored.
FORBIDDEN_IDS = (
    'act', 'root_analysis', 'source_action', 'protocol', 'protocol_action',
    'smk_source', 'smk_action', 'bug_report', 'document_version',
)
MISSING_ID = 987654


class BoardTaskShapeTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.status = TaskStatus.objects.get(code='IN_PROGRESS')
        # A real card with its real task; a second card is the free one the
        # shape tests hang new rows on.
        self.first_card = self.card('Первая')
        self.free_card = self.card('Вторая')
        Task.objects.filter(board_card=self.free_card).delete()

    def _task(self, **overrides):
        values = {
            'source_type': Task.SourceType.BOARD,
            'board_card': self.free_card,
            'department': self.department,
            'task_text': 'Работа',
            'due_date': due(),
            'created_by': self.owner,
            'status': self.status,
        }
        values.update(overrides)
        return Task(**values)

    def _assert_rejected(self, task, field):
        with self.assertRaises(ValidationError) as caught:
            task.clean()
        self.assertIn(field, caught.exception.message_dict)
        with self.assertRaises(IntegrityError), transaction.atomic():
            task.save()

    def test_valid_shape_is_saved(self):
        task = self._task()
        task.clean()
        task.save()
        self.assertEqual(Task.objects.get(pk=task.pk).board_card, self.free_card)

    def test_board_card_is_required(self):
        self._assert_rejected(self._task(board_card=None), 'board_card')

    def test_department_is_free(self):
        # A board is shared work of several departments: a new card names
        # none, and a task saved before that keeps the one it has.
        for department in (None, self.department):
            with self.subTest(department=department):
                Task.objects.filter(board_card=self.free_card).delete()
                task = self._task(department=department)
                task.clean()
                task.save()
                self.assertEqual(Task.objects.get(pk=task.pk).department, department)

    def test_every_foreign_relation_is_refused(self):
        for name in FORBIDDEN_IDS:
            with self.subTest(relation=name):
                self._assert_rejected(self._task(**{f'{name}_id': MISSING_ID}), name)

    def test_individual_assignee_is_refused(self):
        self._assert_rejected(self._task(individual_assignee=self.member), 'individual_assignee')

    def test_workflow_stage_is_refused(self):
        self._assert_rejected(
            self._task(workflow_stage=Task.WorkflowStage.KO_REVIEW), 'workflow_stage',
        )

    def test_board_card_on_another_source_type_is_refused(self):
        task = self._task(
            source_type=Task.SourceType.BUG, bug_report_id=MISSING_ID, department=None,
        )
        self._assert_rejected(task, 'board_card')

    def test_one_task_per_card(self):
        duplicate = self._task(board_card=self.first_card)
        with self.assertRaises(IntegrityError), transaction.atomic():
            duplicate.save()
