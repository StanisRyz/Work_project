"""«Сумма в колонке» (`BoardField.sum_in_column`): the limit, the kind, the
column headers under the filters, «Итого» of «Таблица» and its Excel, and the
number of queries."""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from realtime.testing import capture_realtime_events

from ..selectors import board_fields, build_board_state, parse_board_filters
from ..services import (
    MAX_SUMMED_FIELDS,
    BoardError,
    archive_field,
    complete_card,
    create_field,
    restore_field,
    update_field,
)
from .helpers import BoardFixtureMixin, board_url
from .test_lifecycle import main_of
from .test_table import read_xlsx


class SumsFixture(BoardFixtureMixin):
    def setUp(self):
        self.quantity = create_field(self.board, actor=self.owner, name='Кол-во', kind='NUMBER', sum_in_column=True)
        self.amount = create_field(self.board, actor=self.owner, name='Сумма', kind='NUMBER', sum_in_column=True)
        self.order = create_field(self.board, actor=self.owner, name='Заказ', kind='TEXT')

    def valued(self, title, quantity, amount, **extra):
        return self.card(title, field_values={self.quantity.pk: quantity, self.amount.pk: amount}, **extra)

    def sums_of(self, state, column):
        row = next(row for row in state['columns'] if row['pk'] == column.pk)
        return [(item['name'], item['value']) for item in row['sums']]


class SumFlagTests(SumsFixture, TestCase):
    def test_a_number_only_and_at_most_two_per_board(self):
        self.assertEqual(MAX_SUMMED_FIELDS, 2)
        with self.assertRaisesMessage(BoardError, 'только для поля вида «Число»'):
            update_field(self.order, actor=self.owner, name='Заказ', show_on_tile=True, sum_in_column=True)
        with self.assertRaisesMessage(BoardError, 'только для поля вида «Число»'):
            create_field(self.board, actor=self.owner, name='Дата', kind='DATE', sum_in_column=True)
        with self.assertRaisesMessage(BoardError, 'не больше чем по 2 полям'):
            create_field(self.board, actor=self.owner, name='Вес', kind='NUMBER', sum_in_column=True)
        weight = create_field(self.board, actor=self.owner, name='Вес', kind='NUMBER')
        with self.assertRaisesMessage(BoardError, 'не больше чем по 2 полям'):
            update_field(weight, actor=self.owner, name='Вес', show_on_tile=True, sum_in_column=True)
        # Turning one off makes room; saving a summed field again is no new sum.
        update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, sum_in_column=False)
        update_field(weight, actor=self.owner, name='Вес', show_on_tile=True, sum_in_column=True)
        update_field(weight, actor=self.owner, name='Вес, кг', show_on_tile=True)
        weight.refresh_from_db()
        self.assertTrue(weight.sum_in_column)

    def test_an_archived_field_is_summed_nowhere_and_comes_back_without_its_sum(self):
        archive_field(self.amount, actor=self.owner)
        self.amount.refresh_from_db()
        self.assertFalse(self.amount.sum_in_column)
        create_field(self.board, actor=self.owner, name='Вес', kind='NUMBER', sum_in_column=True)
        restore_field(self.amount, actor=self.owner)
        self.amount.refresh_from_db()
        self.assertFalse(self.amount.sum_in_column)

    def test_a_change_is_a_structure_change_and_the_same_is_nothing(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, sum_in_column=False)
            self.assertEqual(
                [event.data['change'] for event in publisher.events if event.event_type == 'board.updated'],
                ['structure_changed'],
            )
            publisher.clear()
            with self.captureOnCommitCallbacks(execute=True):
                update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, sum_in_column=False)
            self.assertEqual(publisher.events, [])

    def test_the_fields_page_offers_the_box_and_says_the_limit(self):
        self.client.force_login(self.owner)
        url = reverse('boards:field_create', args=[self.board.pk])
        # Two are summed already: the third comes back with the reason.
        response = self.client.post(url, {'name': 'Вес', 'kind': 'NUMBER', 'sum_in_column': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('не больше чем по 2 полям', main_of(response))
        update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, sum_in_column=False)
        response = self.client.post(url, {'name': 'Вес', 'kind': 'NUMBER', 'sum_in_column': '1'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(self.board.fields.get(name='Вес').sum_in_column)
        content = main_of(self.client.get(reverse('boards:fields', args=[self.board.pk])))
        self.assertIn('Сумма в колонке', content)


class ColumnSumTests(SumsFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.valued('Первая', '1000', '2 000 000,50')
        self.valued('Вторая', '250', '1400000', assignees=[self.colleague])
        self.valued('В работе', '7', '3', stage='IN_PROGRESS')
        self.card('Без значений')
        done = self.valued('Готовая', '5', '10')
        complete_card(done, actor=self.member, execution_comment='Готово')

    def state(self, **params):
        fields = board_fields(self.board)
        return build_board_state(
            self.board, self.main, self.member, filters=parse_board_filters(params, fields), fields=fields,
        )

    def test_each_column_sums_the_cards_it_shows(self):
        state = self.state()
        self.assertEqual(
            self.sums_of(state, self.column('TODO')),
            [('Кол-во', Decimal('1250')), ('Сумма', Decimal('3400000.50'))],
        )
        self.assertEqual(self.sums_of(state, self.column('IN_PROGRESS')), [('Кол-во', 7), ('Сумма', 3)])
        # An empty column says nothing; the closing column sums its completed cards.
        self.assertEqual(self.sums_of(state, self.column('REVIEW')), [])
        done = next(row for row in state['columns'] if row['is_done'])
        self.assertEqual([item['value'] for item in done['sums']], [5, 10])

    def test_the_filters_narrow_the_sums(self):
        state = self.state(mine='1')
        self.assertEqual(self.sums_of(state, self.column('TODO')), [('Кол-во', 1000), ('Сумма', Decimal('2000000.50'))])
        state = self.state(q='Вторая')
        self.assertEqual(self.sums_of(state, self.column('TODO')), [('Кол-во', 250), ('Сумма', 1400000)])

    def test_the_header_reads_the_numbers_as_the_board_writes_them(self):
        self.client.force_login(self.member)
        content = main_of(self.client.get(board_url(self.board)))
        self.assertIn('Σ <span class="board-column__sum"><span class="user-text">Кол-во</span>: 1 250</span>', content)
        self.assertIn('Сумма</span>: 3 400 000,5', content)

    def test_the_table_total_and_the_excel_row(self):
        self.client.force_login(self.member)
        url = board_url(self.board)
        content = main_of(self.client.get(url, {'view': 'table'}))
        footer = content.split('<tfoot>', 1)[1].split('</tfoot>', 1)[0]
        self.assertIn('Итого', footer)
        self.assertIn('1 262', footer)
        rows, _ = read_xlsx(self.client.get(url, {'view': 'table', 'export': 'xlsx'}).content)
        header = [cell[1] for cell in rows[0]]
        total = rows[-1]
        self.assertEqual(total[0], ('s', 'Итого'))
        self.assertEqual(total[header.index('Кол-во')], ('n', Decimal('1262')))
        self.assertEqual(total[header.index('Сумма')], ('n', Decimal('3400013.5')))
        self.assertEqual(total[header.index('Заказ')], ('', None))
        # The filtered table sums what it shows.
        rows, _ = read_xlsx(self.client.get(url, {'view': 'table', 'export': 'xlsx', 'mine': '1', 'q': 'Первая'}).content)
        self.assertEqual(rows[-1][header.index('Кол-во')], ('n', Decimal('1000')))

    def test_one_aggregate_whatever_the_cards_and_none_without_a_summed_field(self):
        self.client.force_login(self.member)

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(board_url(self.board))
            return len(queries)

        with_sums = count()
        for index in range(5):
            self.valued(f'Ещё {index}', str(index), '1', stage='REVIEW')
        self.assertEqual(count(), with_sums)
        update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, sum_in_column=False)
        self.assertEqual(count(), with_sums)
        update_field(self.quantity, actor=self.owner, name='Кол-во', show_on_tile=True, sum_in_column=False)
        self.assertEqual(count(), with_sums - 1)
