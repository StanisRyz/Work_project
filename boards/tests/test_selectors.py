"""What a board shows: columns derived from the tasks, at a constant query cost."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from references.models import TaskStatus
from tasks.models import Task
from tasks.services import complete_task, reopen_task

from ..columns import COLUMNS, DONE
from ..models import BoardCard
from ..selectors import boards_for_user, build_board_state
from ..services import create_board
from .helpers import BoardFixtureMixin


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


class BoardStateTests(BoardFixtureMixin, TestCase):
    def _titles(self, state):
        return {
            column['code']: [item['card'].title for item in column['cards']]
            for column in state['columns']
        }

    def test_columns_follow_columns_py(self):
        state = build_board_state(self.board, self.member)
        self.assertEqual([column['code'] for column in state['columns']], [c.code for c in COLUMNS])
        self.assertTrue(state['can_work'])
        self.assertFalse(state['can_manage'])
        outsider_state = build_board_state(self.board, self.outsider)
        self.assertFalse(outsider_state['can_work'])

    def test_work_columns_are_in_position_order(self):
        self.card('A')
        self.card('B')
        self.card('C', stage=BoardCard.Stage.IN_PROGRESS)
        titles = self._titles(build_board_state(self.board, self.member))
        self.assertEqual(titles['TODO'], ['A', 'B'])
        self.assertEqual(titles['IN_PROGRESS'], ['C'])
        self.assertEqual(titles[DONE], [])

    def test_card_completed_from_the_task_page_is_done_and_comes_back_on_reopen(self):
        card = self.card('Сделать отчёт', stage=BoardCard.Stage.REVIEW)
        # Completed the way the task page completes it — not through the board.
        complete_task(task_of(card), self.member, 'Отчёт отправлен')
        titles = self._titles(build_board_state(self.board, self.member))
        self.assertEqual(titles[DONE], ['Сделать отчёт'])
        self.assertEqual(titles['REVIEW'], [])

        reopen_task(task_of(card), self.admin)
        titles = self._titles(build_board_state(self.board, self.member))
        self.assertEqual(titles[DONE], [])
        self.assertEqual(titles['REVIEW'], ['Сделать отчёт'])

    def test_done_is_newest_completion_first(self):
        first, second = self.card('Первая'), self.card('Вторая')
        complete_task(task_of(first), self.member, 'Да')
        complete_task(task_of(second), self.member, 'Да')
        titles = self._titles(build_board_state(self.board, self.member))
        self.assertEqual(titles[DONE], ['Вторая', 'Первая'])

    def test_cancelled_card_is_on_no_column(self):
        card = self.card('Отменённая')
        Task.objects.filter(pk=task_of(card).pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'),
        )
        titles = self._titles(build_board_state(self.board, self.member))
        self.assertNotIn('Отменённая', sum(titles.values(), []))

    def test_card_carries_task_assignees_and_due_date(self):
        card = self.card(assignees=[self.member, self.colleague])
        item = build_board_state(self.board, self.member)['columns'][0]['cards'][0]
        self.assertEqual(item['card'], card)
        self.assertEqual(item['task'], task_of(card))
        self.assertEqual({user.pk for user in item['assignees']}, {self.member.pk, self.colleague.pk})
        self.assertEqual(item['due_date'], task_of(card).due_date)

    def _query_count(self):
        # A fresh user object each time, so nothing cached on it skews the count.
        user = type(self.member).objects.get(pk=self.member.pk)
        with CaptureQueriesContext(connection) as queries:
            build_board_state(self.board, user)
        return len(queries)

    def test_query_count_does_not_depend_on_card_count(self):
        self.card('Одна')
        baseline = self._query_count()
        for index in range(6):
            self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
        complete_task(task_of(self.card('Готовая')), self.member, 'Да')
        user = type(self.member).objects.get(pk=self.member.pk)
        with self.assertNumQueries(baseline):
            build_board_state(self.board, user)


class BoardsForUserTests(BoardFixtureMixin, TestCase):
    def test_only_boards_the_user_is_a_member_of(self):
        create_board(
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
        )
        self.assertEqual(list(boards_for_user(self.member)), [self.board])
        self.assertEqual(list(boards_for_user(self.outsider)), [])
        self.assertEqual(
            [board.name for board in boards_for_user(self.owner)], ['Другая', 'Планирование'],
        )
