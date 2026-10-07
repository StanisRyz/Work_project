"""Stage 17: «Таблица» of a sub-board and its Excel.

`?view=table` on the sub-board's own address: the cards as rows, every live
field of the board a column, ordered by a whitelist, filtered by the board's
own filters; `&export=xlsx` is that very state written by `ecosystem.xlsx`.
Read only — reading the board is the right.
"""

import datetime
import io
import re
import zipfile
from decimal import Decimal
from unittest import mock
from xml.etree import ElementTree

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ecosystem.xlsx import build_xlsx
from tasks.services import complete_task

from ..models import BoardCard
from ..selectors import BoardFilters, build_board_table, parse_table_sort
from ..services import (
    create_field,
    archive_field,
    archive_option,
    cancel_card,
    create_sub_board,
    move_card,
    set_column_stale_days,
)
from ..views import export_filename_stem, safe_file_part
from .helpers import board_url, column_of, done_column_of, new_card
from .test_access import AccessFixture
from .test_column_time import age, at_days_ago
from .test_fields import S, T, FieldsMixin
from .test_journal import task_of


NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def read_xlsx(content):
    """The sheet as rows of `(kind, value)` cells — kind `s` (text), `n`
    (number), `d` (a typed date, read back as a `date`) or `''` (empty)."""
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        sheet = ElementTree.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        styles = archive.read('xl/styles.xml').decode() if 'xl/styles.xml' in archive.namelist() else ''
    rows = []
    for row in sheet.find('x:sheetData', NS):
        cells = []
        for cell in row:
            value = cell.find('x:v', NS)
            text = cell.find('x:is/x:t', NS)
            if text is not None:
                cells.append(('s', text.text or ''))
            elif value is None:
                cells.append(('', None))
            elif cell.get('s') == '1':
                cells.append(('d', datetime.date(1899, 12, 30) + datetime.timedelta(days=int(value.text))))
            else:
                cells.append(('n', Decimal(value.text)))
        rows.append(cells)
    return rows, styles


def values(rows):
    return [[value for _, value in row] for row in rows]


class TableMixin(FieldsMixin):
    """The pilot's fields and five cards on «Основная», one more on «Цех ПиР».

    | card   | column      | Номер заявки | Срок изг.  | Приоритет | Сумма | state |
    | ------ | ----------- | ------------ | ---------- | --------- | ----- | ----- |
    | Alpha  | Сделать     | 3-1579       | 2026-11-30 | Высокий   | 10    | open, 5 days, overdue |
    | Beta   | В работе    | —            | 2026-12-01 | Низкий    | 2,5   | open, today |
    | Gamma  | Сделать     | 7-0001       | —          | Средний   | —     | open, 1 day |
    | Delta  | Готово      | —            | —          | Высокий   | 100   | completed |
    | Omega  | —           | —            | —          | —         | —     | cancelled |
    | Pir    | (Цех ПиР)   | —            | —          | —         | —     | open |
    """

    def make_cards(self):
        self.make_fields()
        self.alpha = self.card(
            'Alpha', due_date=timezone.localdate() - datetime.timedelta(days=1),
            field_values={
                self.order.pk: '3-1579', self.deadline.pk: '2026-11-30',
                self.priority.pk: str(self.high.pk), self.amount.pk: '10',
            },
        )
        age(self.alpha, days=5)
        self.beta = self.card(
            'beta', assignees=[self.colleague], stage='IN_PROGRESS',
            due_date=timezone.localdate() + datetime.timedelta(days=9),
            field_values={self.deadline.pk: '2026-12-01', self.priority.pk: str(self.low.pk), self.amount.pk: '2,5'},
        )
        self.gamma = self.card(
            'Gamma', due_date=timezone.localdate() + datetime.timedelta(days=3),
            field_values={self.order.pk: '7-0001', self.priority.pk: str(self.middle.pk)},
        )
        age(self.gamma, days=1)
        self.delta = self.card(
            'Delta', due_date=timezone.localdate() + datetime.timedelta(days=2),
            field_values={self.priority.pk: str(self.high.pk), self.amount.pk: '100'},
        )
        complete_task(task_of(self.delta), self.member, 'Готово')
        self.omega = self.card('Omega')
        cancel_card(self.omega, actor=self.owner, reason='Ошибка')
        self.pir = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        self.pir_card = new_card(
            self.board, self.member, 'Pir', assignees=[self.member], column=column_of(self.board, 'TODO', self.pir),
        )

    def table(self, user=None, **options):
        filters = options.pop('filters', BoardFilters())
        return build_board_table(self.board, self.main, user or self.member, filters=filters, **options)

    def titles(self, state):
        return [row['card'].title for row in state['rows']]

    def url(self, **query):
        query = {'view': 'table', **query}
        return board_url(self.board) + '?' + '&'.join(f'{key}={value}' for key, value in query.items())


class TableRowsTests(TableMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def test_open_and_completed_in_the_boards_order_without_the_cancelled(self):
        state = self.table()
        self.assertEqual(self.titles(state), ['Alpha', 'Gamma', 'beta', 'Delta'])
        delta = state['rows'][-1]
        self.assertEqual(delta['column'], done_column_of(self.board))
        self.assertEqual(delta['status_label'], 'Выполнена')
        self.assertIsNone(delta['in_column_days'])
        self.assertEqual(delta['completed'], timezone.localdate())
        alpha = state['rows'][0]
        self.assertEqual((alpha['column_label'], alpha['status_label'], alpha['in_column_days']), ('Сделать', 'В работе', 5))

    def test_every_completed_one_whatever_the_done_limit(self):
        for index in range(3):
            card = self.card(f'Done {index}')
            complete_task(task_of(card), self.member, 'Готово')
        with mock.patch('boards.selectors.DONE_LIMIT', 1):
            self.assertEqual(len(self.table()['rows']), 7)

    def test_the_cancelled_with_the_flag(self):
        state = self.table(cancelled=True)
        self.assertEqual(self.titles(state)[-1], 'Omega')
        omega = state['rows'][-1]
        self.assertEqual((omega['column'], omega['column_label'], omega['status_label']), (None, 'Отменена', 'Отменена'))

    def test_the_whole_board(self):
        state = self.table(whole_board=True)
        self.assertEqual(self.titles(state), ['Alpha', 'Gamma', 'beta', 'Delta', 'Pir'])
        self.assertEqual(state['rows'][-1]['sub_board'].name, 'Цех ПиР')
        self.assertEqual(self.titles(self.table()), ['Alpha', 'Gamma', 'beta', 'Delta'])

    def test_only_live_fields_are_columns(self):
        archive_field(self.customer, actor=self.owner)
        names = [field.name for field in self.table()['field_columns']]
        self.assertEqual(names, ['Номер заявки', 'Срок изг.', 'Приоритет', 'Стоп', 'Сумма'])

    def test_the_cells(self):
        alpha = self.table()['rows'][0]
        by_name = {field.name: cell for field, cell in zip(self.table()['field_columns'], alpha['cells'])}
        self.assertEqual(by_name['Номер заявки']['raw'], '3-1579')
        self.assertEqual(by_name['Срок изг.']['raw'], datetime.date(2026, 11, 30))
        self.assertEqual(by_name['Приоритет']['raw'], 'Высокий')
        self.assertEqual(by_name['Сумма']['raw'], Decimal('10'))
        self.assertIsNone(by_name['Стоп']['raw'])
        archive_option(self.high, actor=self.owner)
        alpha = self.table()['rows'][0]
        self.assertEqual(alpha['cells'][3]['raw'], 'Высокий (в архиве)')


class TableSortTests(TableMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def sorted_titles(self, sort, **options):
        return self.titles(self.table(sort=parse_table_sort(sort, self.fields()), **options))

    def fields(self):
        from ..selectors import board_fields

        return board_fields(self.board)

    def test_each_column_both_ways(self):
        cases = {
            'code': ['Alpha', 'beta', 'Gamma', 'Delta'],
            'title': ['Alpha', 'beta', 'Delta', 'Gamma'],
            'column': ['Alpha', 'Gamma', 'beta', 'Delta'],
            'due': ['Alpha', 'Delta', 'Gamma', 'beta'],
            'created': ['Alpha', 'beta', 'Gamma', 'Delta'],
        }
        for key, expected in cases.items():
            with self.subTest(sort=key):
                self.assertEqual(self.sorted_titles(key), expected)
                self.assertEqual(self.sorted_titles(f'-{key}'), expected[::-1])

    def test_days_and_completed_put_the_empty_last_both_ways(self):
        self.assertEqual(self.sorted_titles('days'), ['beta', 'Gamma', 'Alpha', 'Delta'])
        self.assertEqual(self.sorted_titles('-days'), ['Alpha', 'Gamma', 'beta', 'Delta'])
        self.assertEqual(self.sorted_titles('completed')[0], 'Delta')
        self.assertEqual(self.sorted_titles('-completed')[0], 'Delta')

    def test_a_field_of_each_kind(self):
        key = lambda field: f'field_{field.pk}'  # noqa: E731
        # A list by the options' place: Высокий, Средний, Низкий; empty last.
        self.assertEqual(self.sorted_titles(key(self.priority)), ['Alpha', 'Delta', 'Gamma', 'beta'])
        self.assertEqual(self.sorted_titles('-' + key(self.priority)), ['beta', 'Gamma', 'Alpha', 'Delta'])
        # A number as a number: 2,5 < 10 < 100.
        self.assertEqual(self.sorted_titles(key(self.amount)), ['beta', 'Alpha', 'Delta', 'Gamma'])
        self.assertEqual(self.sorted_titles('-' + key(self.amount)), ['Delta', 'Alpha', 'beta', 'Gamma'])
        # A date as a date.
        self.assertEqual(self.sorted_titles(key(self.deadline)), ['Alpha', 'beta', 'Gamma', 'Delta'])
        self.assertEqual(self.sorted_titles('-' + key(self.deadline)), ['beta', 'Alpha', 'Gamma', 'Delta'])
        # A text whatever the case.
        self.assertEqual(self.sorted_titles(key(self.order)), ['Alpha', 'Gamma', 'beta', 'Delta'])
        self.assertEqual(self.sorted_titles('-' + key(self.order)), ['Gamma', 'Alpha', 'beta', 'Delta'])

    def test_an_unknown_sort_is_ignored(self):
        default = self.sorted_titles('')
        for sort in ('pk', '-board_card__title', 'field_999', f'field_{self.customer.pk}x', 'title; DROP', '--code'):
            with self.subTest(sort=sort):
                self.assertEqual(parse_table_sort(sort, self.fields()), '')
                self.assertEqual(self.sorted_titles(sort), default)
        archive_field(self.order, actor=self.owner)
        self.assertEqual(parse_table_sort(f'field_{self.order.pk}', self.fields()), '')

    def test_the_page_sorts_and_draws_the_arrow(self):
        self.client.force_login(self.member)
        page = self.client.get(self.url(sort=f'-field_{self.amount.pk}')).content.decode()
        body = page.split('<tbody>', 1)[1]
        self.assertLess(body.index('>Delta<'), body.index('>Alpha<'))
        self.assertIn('aria-sort="descending"', page)
        unknown = self.client.get(self.url(sort='-board_card__title')).content.decode()
        self.assertNotIn('aria-sort="descending"', unknown)
        self.assertNotIn('name="sort"', unknown)


class TableFilterTests(TableMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def test_mine_q_and_a_field(self):
        self.assertEqual(self.titles(self.table(filters=BoardFilters(mine=True))), ['Alpha', 'Gamma', 'Delta'])
        self.assertEqual(self.titles(self.table(filters=BoardFilters(q='3-1579'))), ['Alpha'])
        self.client.force_login(self.member)
        page = self.client.get(self.url(**{f'f_{self.priority.pk}': self.high.pk})).content.decode()
        body = page.split('<tbody>', 1)[1]
        self.assertIn('>Alpha<', body)
        self.assertIn('>Delta<', body)
        self.assertNotIn('>Gamma<', body)

    def test_overdue_and_stale_keep_open_work_only(self):
        self.assertEqual(self.titles(self.table(filters=BoardFilters(overdue=True))), ['Alpha'])
        self.assertEqual(self.titles(self.table(filters=BoardFilters(stale=True))), [])
        set_column_stale_days(column_of(self.board, 'TODO'), actor=self.owner, days=3)
        state = self.table(filters=BoardFilters(stale=True), cancelled=True)
        self.assertEqual(self.titles(state), ['Alpha'])
        self.assertTrue(state['rows'][0]['is_stale'])

    def test_the_filter_travels_with_the_switch_and_the_tabs(self):
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'mine': '1'}).content.decode()
        self.assertIn(f'href="{board_url(self.board)}?view=table&amp;mine=1"', page)
        self.assertIn(f'{board_url(self.board)}?view=table&amp;mine=1&amp;export=xlsx', page)
        table = self.client.get(self.url(mine=1, scope='board', sort='title')).content.decode()
        self.assertIn(f'href="{board_url(self.board)}?mine=1"', table)
        self.assertIn(
            f'{board_url(self.board, self.pir)}?view=table&amp;scope=board&amp;sort=title&amp;mine=1', table,
        )
        self.assertIn('name="scope" value="board" checked', table)
        self.assertIn('name="sort" value="title"', table)
        # «Сбросить» drops the filter and keeps the table's own options.
        self.assertIn(
            f'class="board-filters__reset" href="{board_url(self.board)}?view=table&amp;scope=board&amp;sort=title"',
            table,
        )


class TablePageTests(TableMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def test_the_page(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertTemplateUsed(response, 'boards/table.html')
        self.assertIn('page-container--fill', page)
        self.assertIn('class="table-card act-table-card board-table-card"', page)
        self.assertIn('Обновлено', page)
        self.assertIn('Поддоска «Основная»: 4 карточки', page)
        self.assertIn(f'href="{board_url(self.board)}?view=table">Обновить</a>', page)
        card_url = f'{board_url(self.board)}?card={self.alpha.pk}'
        self.assertIn(f'data-row-url="{card_url}"', page)
        self.assertIn(f'<a class="table-link" href="{card_url}"', page)
        self.assertIn(f'>{self.alpha.code}</a>', page)
        self.assertIn('board-chip--red', page)
        self.assertIn('5 дн.', page)
        self.assertNotIn('Поддоска</th>', page)
        whole = self.client.get(self.url(scope='board')).content.decode()
        self.assertIn('Поддоска</th>', whole)
        self.assertIn(f'data-row-url="{board_url(self.board, self.pir)}?card={self.pir_card.pk}"', whole)

    def test_a_reader_reads_it_and_nothing_is_written(self):
        self.client.force_login(self.member)
        before = BoardCard.objects.order_by('pk').values_list('pk', 'version', 'updated_at')
        before = list(before)
        self.client.get(self.url(cancelled=1, scope='board'))
        self.client.get(self.url(export='xlsx'))
        self.assertEqual(list(BoardCard.objects.order_by('pk').values_list('pk', 'version', 'updated_at')), before)

    def test_the_query_count_does_not_grow(self):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url(scope='board', cancelled=1))
        extra = create_field(self.board, actor=self.owner, name='Ещё текст', kind=T)
        stage = create_field(
            self.board, actor=self.owner, name='Этап', kind=S, options=[('Один', 'blue'), ('Два', 'green')],
        )
        for index in range(8):
            card = self.card(
                f'More {index}',
                field_values={
                    self.order.pk: f'9-{index}', self.priority.pk: str(self.low.pk),
                    self.amount.pk: str(index), self.deadline.pk: '2026-12-24',
                    extra.pk: f'текст {index}', stage.pk: str(stage.options.first().pk),
                },
            )
            if index % 3 == 0:
                move_card(card, actor=self.member, column=column_of(self.board, 'REVIEW'))
            if index % 4 == 1:
                complete_task(task_of(card), self.member, 'Готово')
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.url(scope='board', cancelled=1))
        self.assertIn('More 7', response.content.decode())
        self.assertIn('Этап', response.content.decode())
        self.assertEqual(len(many), len(few))
        with CaptureQueriesContext(connection) as export:
            self.client.get(self.url(scope='board', cancelled=1, export='xlsx'))
        self.assertLess(len(export), len(many))


class TableAccessTests(AccessFixture, TestCase):
    def test_an_outsider_is_refused_the_table_and_its_excel(self):
        url = reverse('boards:sub_board', args=[self.board.pk, self.card.sub_board_id])
        self.client.force_login(self.pdo)
        for query in ({'view': 'table'}, {'view': 'table', 'export': 'xlsx'}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(url, query).status_code, 403)
        self.client.force_login(self.otk)
        self.assertEqual(self.client.get(url, {'view': 'table'}).status_code, 200)
        self.assertEqual(self.client.get(url, {'view': 'table', 'export': 'xlsx'}).status_code, 200)


# --------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------


class TableExcelTests(TableMixin, TestCase):
    def setUp(self):
        self.make_cards()
        self.client.force_login(self.member)

    def export(self, **query):
        response = self.client.get(self.url(export='xlsx', **query))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response['Content-Type'], 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        return response

    def test_the_rows_are_the_screens(self):
        for query in ({}, {'sort': '-title'}, {'cancelled': 1, 'scope': 'board'}, {'mine': 1, 'sort': 'due'}):
            with self.subTest(query=query):
                rows, _ = read_xlsx(self.export(**query).content)
                page = self.client.get(self.url(**query)).content.decode()
                screen = re.findall(r'<a class="table-link" href="[^"]*" title="[^"]*">([^<]+)</a>', page)
                self.assertEqual([row[0][1] for row in rows[1:]], screen)

    def test_headers_and_cell_types(self):
        rows, styles = read_xlsx(self.export().content)
        self.assertEqual(values(rows)[0], [
            'Код', 'Название', 'Колонка', 'Статус', 'Исполнители', 'Срок',
            'Исходный срок', 'Переносов', 'Последняя причина', 'В колонке, дн.',
            'Чек-лист', 'Номер заявки', 'Заказ покупателя', 'Срок изг.', 'Приоритет', 'Стоп', 'Сумма',
            'Создана', 'Завершена',
        ])
        self.assertIn('formatCode="dd.mm.yyyy"', styles)
        alpha = rows[1]
        self.assertEqual(alpha[0], ('s', self.alpha.code))
        self.assertEqual(alpha[2], ('s', 'Сделать'))
        self.assertEqual(alpha[3], ('s', 'В работе'))
        self.assertEqual(alpha[4], ('s', 'member_one'))
        self.assertEqual(alpha[5], ('d', timezone.localdate() - datetime.timedelta(days=1)))
        # Stage 21: «Исходный срок» (the срок the card was created with),
        # «Переносов» (a number, 0 when never moved), «Последняя причина»
        # (empty when never moved).
        self.assertEqual(alpha[6], ('d', timezone.localdate() - datetime.timedelta(days=1)))
        self.assertEqual(alpha[7], ('n', Decimal(0)))
        self.assertEqual(alpha[8], ('', None))
        self.assertEqual(alpha[9], ('n', Decimal(5)))
        # «Чек-лист»: no items, an empty cell (filled ones: `test_checklist.py`).
        self.assertEqual(alpha[10], ('', None))
        self.assertEqual(alpha[11], ('s', '3-1579'))
        self.assertEqual(alpha[12], ('', None))
        self.assertEqual(alpha[13], ('d', datetime.date(2026, 11, 30)))
        self.assertEqual(alpha[14], ('s', 'Высокий'))
        self.assertEqual(alpha[15], ('', None))
        self.assertEqual(alpha[16], ('n', Decimal(10)))
        self.assertEqual(alpha[17], ('d', timezone.localdate()))
        self.assertEqual(alpha[18], ('', None))
        beta = rows[3]
        self.assertEqual(beta[16], ('n', Decimal('2.5')))
        delta = rows[4]
        self.assertEqual((delta[2], delta[3], delta[9], delta[18]), (
            ('s', 'Готово'), ('s', 'Выполнена'), ('', None), ('d', timezone.localdate()),
        ))

    def test_names_are_written_by_name_and_the_archived_option_marked(self):
        self.member.first_name, self.member.last_name = 'Иван', 'Петров'
        self.member.save()
        archive_option(self.high, actor=self.owner)
        rows, _ = read_xlsx(self.export().content)
        self.assertEqual(rows[1][4], ('s', 'Иван Петров'))
        self.assertEqual(rows[1][14], ('s', 'Высокий (в архиве)'))

    def test_empty_is_an_empty_cell_never_none_or_a_dash(self):
        content = self.export(cancelled=1).content
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            sheet = archive.read('xl/worksheets/sheet1.xml').decode()
        self.assertNotIn('None', sheet)
        self.assertNotIn('>—<', sheet)
        self.assertNotIn('<v></v>', sheet)
        rows, _ = read_xlsx(content)
        omega = rows[-1]
        self.assertEqual((omega[2], omega[3], omega[9]), (('s', 'Отменена'), ('s', 'Отменена'), ('', None)))

    def test_the_whole_board_adds_the_sub_board_after_the_code(self):
        rows, _ = read_xlsx(self.export(scope='board').content)
        self.assertEqual(values(rows)[0][:3], ['Код', 'Поддоска', 'Название'])
        self.assertEqual(rows[-1][1], ('s', 'Цех ПиР'))

    def test_the_file_name(self):
        stamp = timezone.localdate().strftime('%Y-%m-%d')
        disposition = self.export()['Content-Disposition']
        self.assertEqual(disposition, f'attachment; filename="{self.board.code}-Osnovnaya-{stamp}.xlsx"')
        disposition = self.export(scope='board')['Content-Disposition']
        self.assertEqual(disposition, f'attachment; filename="{self.board.code}-vse-poddoski-{stamp}.xlsx"')
        self.assertRegex(disposition, r'^attachment; filename="[A-Za-z0-9-]+\.xlsx"$')

    def test_safe_file_parts(self):
        self.assertEqual(safe_file_part('ЗАП'), 'ZAP')
        self.assertEqual(safe_file_part('Цех ПиР / 2'), 'Tsekh-PiR-2')
        self.assertEqual(safe_file_part('Щука «ёж»'), 'Shchuka-ezh')
        self.assertEqual(safe_file_part('../..'), '')
        sub_board = self.main
        sub_board.name = '???'
        self.assertEqual(export_filename_stem(self.board, sub_board, whole_board=False), self.board.code)


class XlsxWriterTests(TestCase):
    def test_decimals_are_numbers_and_dates_text_unless_typed(self):
        plain, _ = read_xlsx(build_xlsx('Лист', ['A', 'B'], [[Decimal('1234.5000'), datetime.date(2026, 10, 6)]]))
        self.assertEqual(plain[1], [('n', Decimal('1234.5')), ('s', '06.10.2026')])
        typed, styles = read_xlsx(
            build_xlsx('Лист', ['A', 'B'], [[Decimal('1E+2'), datetime.date(2026, 10, 6)]], typed_dates=True),
        )
        self.assertEqual(typed[1], [('n', Decimal('100')), ('d', datetime.date(2026, 10, 6))])
        self.assertIn('numFmtId="164"', styles)

    def test_the_package_with_styles_is_well_formed(self):
        content = build_xlsx('Лист', ['A'], [[datetime.date(2026, 1, 1)]], typed_dates=True)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertIn('xl/styles.xml', archive.namelist())
            self.assertIn('/xl/styles.xml', archive.read('[Content_Types].xml').decode())
            self.assertIn('Target="styles.xml"', archive.read('xl/_rels/workbook.xml.rels').decode())
            for name in archive.namelist():
                ElementTree.fromstring(archive.read(name))
