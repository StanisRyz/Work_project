"""What a sub-board shows: columns derived from the tasks, at a constant query cost."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from references.models import TaskStatus
from tasks.models import Task
from tasks.services import complete_task, reopen_task

from ..columns import DEFAULT_COLUMNS
from ..selectors import build_board_state
from ..services import create_board, create_column, create_sub_board
from .helpers import BoardFixtureMixin, column_of, fresh_code, new_card


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


class BoardStateTests(BoardFixtureMixin, TestCase):
    def _titles(self, state):
        return {
            column['name']: [item['card'].title for item in column['cards']]
            for column in state['columns']
        }

    def test_a_new_board_has_the_default_columns(self):
        state = build_board_state(self.board, self.main, self.member)
        self.assertEqual(
            [(column['name'], column['is_done']) for column in state['columns']],
            list(DEFAULT_COLUMNS),
        )
        self.assertEqual([tab['sub_board'].name for tab in state['sub_boards']], ['Основная'])
        self.assertTrue(state['can_work'])
        self.assertFalse(state['can_manage'])
        outsider_state = build_board_state(self.board, self.main, self.outsider)
        self.assertFalse(outsider_state['can_work'])

    def test_columns_are_the_sub_boards_own(self):
        second = create_sub_board(self.board, actor=self.owner, name='Цех')
        create_column(second, actor=self.owner, name='Проверка ОТК')
        self.card('На основной')
        state = build_board_state(self.board, second, self.member)
        self.assertEqual(
            [column['name'] for column in state['columns']],
            ['Сделать', 'В работе', 'На проверке', 'Проверка ОТК', 'Готово'],
        )
        self.assertEqual(sum(column['count'] for column in state['columns']), 0)
        self.assertEqual(
            [(tab['sub_board'].name, tab['is_active']) for tab in state['sub_boards']],
            [('Основная', False), ('Цех', True)],
        )

    def test_work_columns_are_in_position_order(self):
        self.card('A')
        self.card('B')
        self.card('C', stage='IN_PROGRESS')
        titles = self._titles(build_board_state(self.board, self.main, self.member))
        self.assertEqual(titles['Сделать'], ['A', 'B'])
        self.assertEqual(titles['В работе'], ['C'])
        self.assertEqual(titles['Готово'], [])

    def test_card_completed_from_the_task_page_is_done_and_comes_back_on_reopen(self):
        card = self.card('Сделать отчёт', stage='REVIEW')
        # Completed the way the task page completes it — not through the board.
        complete_task(task_of(card), self.member, 'Отчёт отправлен')
        titles = self._titles(build_board_state(self.board, self.main, self.member))
        self.assertEqual(titles['Готово'], ['Сделать отчёт'])
        self.assertEqual(titles['На проверке'], [])

        reopen_task(task_of(card), self.admin)
        titles = self._titles(build_board_state(self.board, self.main, self.member))
        self.assertEqual(titles['Готово'], [])
        self.assertEqual(titles['На проверке'], ['Сделать отчёт'])

    def test_done_is_newest_completion_first(self):
        first, second = self.card('Первая'), self.card('Вторая')
        complete_task(task_of(first), self.member, 'Да')
        complete_task(task_of(second), self.member, 'Да')
        titles = self._titles(build_board_state(self.board, self.main, self.member))
        self.assertEqual(titles['Готово'], ['Вторая', 'Первая'])

    def test_done_is_limited_and_the_rest_counted(self):
        for index in range(3):
            complete_task(task_of(self.card(f'Готовая {index}')), self.member, 'Да')
        done = build_board_state(self.board, self.main, self.member, done_limit=2)['columns'][-1]
        self.assertEqual([item['card'].title for item in done['cards']], ['Готовая 2', 'Готовая 1'])
        self.assertEqual(done['count'], 3)
        self.assertEqual(done['more'], 1)

    def test_panel_card_is_only_a_card_of_this_board(self):
        card = self.card('Своя')
        other = create_board(
            code=fresh_code(),
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
        )
        foreign = new_card(other, self.owner, 'Чужая', assignees=[self.owner])
        second = create_sub_board(self.board, actor=self.owner, name='Вторая')
        other_tab = new_card(self.board, self.member, 'На другой вкладке', assignees=[self.member],
                             sub_board=second)
        own = build_board_state(self.board, self.main, self.member, card_id=card.pk)['card']
        self.assertEqual(own['card'], card)
        self.assertIsNone(own['moved_to'])
        for value in (foreign.pk, 999999, 'abc', ''):
            with self.subTest(value=value):
                self.assertIsNone(build_board_state(self.board, self.main, self.member, card_id=value)['card'])
        # A card of another tab of the same board — moved there while a page
        # had it open — is shown with where it stands now.
        moved = build_board_state(self.board, self.main, self.member, card_id=other_tab.pk)['card']
        self.assertEqual(moved['card'], other_tab)
        self.assertEqual(moved['moved_to'], second)
        self.assertEqual(moved['column'], column_of(self.board, 'TODO', second))

    def test_panel_finds_a_cancelled_card(self):
        card = self.card('Отменённая')
        Task.objects.filter(pk=task_of(card).pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'),
        )
        item = build_board_state(self.board, self.main, self.member, card_id=card.pk)['card']
        self.assertTrue(item['is_closed'])
        self.assertIsNone(item['column'])

    def test_cancelled_card_is_on_no_column(self):
        card = self.card('Отменённая')
        Task.objects.filter(pk=task_of(card).pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'),
        )
        titles = self._titles(build_board_state(self.board, self.main, self.member))
        self.assertNotIn('Отменённая', sum(titles.values(), []))

    def test_card_carries_task_assignees_and_due_date(self):
        card = self.card(assignees=[self.member, self.colleague])
        item = build_board_state(self.board, self.main, self.member)['columns'][0]['cards'][0]
        self.assertEqual(item['card'], card)
        self.assertEqual(item['task'], task_of(card))
        self.assertEqual({user.pk for user in item['assignees']}, {self.member.pk, self.colleague.pk})
        self.assertEqual(item['due_date'], task_of(card).due_date)

    def _query_count(self):
        # A fresh user object each time, so nothing cached on it skews the count.
        user = type(self.member).objects.get(pk=self.member.pk)
        with CaptureQueriesContext(connection) as queries:
            build_board_state(self.board, self.main, user)
        return len(queries)

    def test_query_count_does_not_depend_on_card_count(self):
        # One open and one done card: an empty list skips its prefetches, so
        # the baseline must already read both lists.
        self.card('Одна')
        complete_task(task_of(self.card('Первая готовая')), self.member, 'Да')
        baseline = self._query_count()
        for index in range(6):
            self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
        for index in range(3):
            complete_task(task_of(self.card(f'Готовая {index}')), self.member, 'Да')
        # Nor on the number of columns and tabs.
        for index in range(4):
            column = create_column(self.main, actor=self.owner, name=f'Этап {index}')
            new_card(self.board, self.member, f'В этапе {index}', assignees=[self.member], column=column)
        create_sub_board(self.board, actor=self.owner, name='Ещё вкладка')
        user = type(self.member).objects.get(pk=self.member.pk)
        with self.assertNumQueries(baseline):
            build_board_state(self.board, self.main, user)
