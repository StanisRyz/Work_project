"""Stage 16: filtering a board by its own card fields, and finding a card by a value.

One parse (`parse_board_filters()` with the board's fields) for the page, its
fragment and a drag's JSON counts; a wrong parameter is dropped, never an
error; every field filter is an `Exists()` of the tasks' own query, so the
number of queries does not move with them. The board search, «Задачи» and the
topbar search find a card by a live text field's value — inside what the
reader may read.
"""

import datetime
from decimal import Decimal
from urllib.parse import urlencode

from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from dashboard.search import quick_search
from tasks.services import complete_task

from ..selectors import (
    NO_FILTERS,
    BoardFilters,
    FieldFilter,
    board_fields,
    build_board_state,
    column_counts,
    parse_board_filters,
)
from ..services import archive_field, archive_option, create_board, create_field
from .helpers import (
    assert_page_matches_fragment,
    board_url,
    column_of,
    done_column_of,
    fragment_url,
    fresh_code,
    new_card,
)
from .test_access import AccessFixture
from .test_fields import D, N, S, T, FieldsMixin
from .test_journal import task_of
from .test_lifecycle import attribute, main_of


# A sub-board page with fields and values and a card open for its
# исполнитель — the page `test_fields.FieldQueryCountTests` measures (43) plus
# the исполнители of a completed card, which that page has none of — under
# any number of field filters.
PAGE_QUERIES = 44


def visible(state):
    """Every card title the columns draw, sorted."""
    return sorted(item['card'].title for column in state['columns'] for item in column['cards'])


def counts(state):
    return {str(column['pk']): column['count'] for column in state['columns']}


class FieldFilterMixin(FieldsMixin):
    """Five cards on the pilot's fields, one of them completed.

    | card     | Номер заявки | Срок изг.  | Приоритет | Стоп | Сумма   | state |
    | -------- | ------------ | ---------- | --------- | ---- | ------- | ----- |
    | Alpha    | 3-1579       | 2026-11-30 | Высокий   | Да   | 1234,5  | open, mine |
    | Beta     | 3-1580       | 2026-12-01 | Средний   | —    | 10      | open, theirs |
    | Gamma    | —            | 2026-11-01 | —         | Да   | —       | open, mine, overdue |
    | Delta    | 7-0001       | —          | Высокий   | —    | 5       | completed, mine |
    | Epsilon  | —            | —          | Низкий    | —    | —       | open, theirs |
    """

    def make_cards(self):
        self.make_fields()
        past = timezone.localdate() - datetime.timedelta(days=3)
        self.alpha = self.card('Alpha', assignees=[self.member], field_values={
            self.order.pk: '3-1579', self.deadline.pk: '2026-11-30', self.priority.pk: self.high.pk,
            self.stop.pk: self.yes.pk, self.amount.pk: '1234,5',
        })
        self.beta = self.card('Beta', assignees=[self.colleague], stage='IN_PROGRESS', field_values={
            self.order.pk: '3-1580', self.deadline.pk: '2026-12-01', self.priority.pk: self.middle.pk,
            self.amount.pk: '10',
        })
        self.gamma = self.card('Gamma', assignees=[self.member], due_date=past, field_values={
            self.deadline.pk: '2026-11-01', self.stop.pk: self.yes.pk,
        })
        self.delta = self.card('Delta', assignees=[self.member], field_values={
            self.order.pk: '7-0001', self.priority.pk: self.high.pk, self.amount.pk: '5',
        })
        complete_task(task_of(self.delta), self.member, 'Готово')
        self.epsilon = self.card('Epsilon', assignees=[self.colleague], stage='REVIEW', field_values={
            self.priority.pk: self.low.pk,
        })

    def parse(self, params, fields=None):
        query = QueryDict(mutable=True)
        for key, value in params.items():
            query.setlist(key, value if isinstance(value, list) else [value])
        return parse_board_filters(query, board_fields(self.board) if fields is None else fields)

    def state(self, params, user=None):
        return build_board_state(self.board, self.main, user or self.member, filters=self.parse(params))

    def titles(self, params):
        return visible(self.state(params))


# --------------------------------------------------------------------------
# The parse
# --------------------------------------------------------------------------


class FieldFilterParseTests(FieldFilterMixin, TestCase):
    def setUp(self):
        self.make_fields()

    def test_each_kind(self):
        f = self.parse({
            f'f_{self.priority.pk}': str(self.high.pk),
            f'f_{self.deadline.pk}_from': '2026-11-01',
            f'f_{self.deadline.pk}_to': '2026-11-30',
            f'f_{self.amount.pk}_min': '5',
            f'f_{self.amount.pk}_max': '100,25',
            f'f_{self.order.pk}': '  3-1579 ',
        })
        self.assertEqual(f.fields, (
            FieldFilter(self.order.pk, T, text='3-1579'),
            FieldFilter(self.deadline.pk, D, date_from=datetime.date(2026, 11, 1), date_to=datetime.date(2026, 11, 30)),
            FieldFilter(self.priority.pk, S, option_ids=(self.high.pk,)),
            FieldFilter(self.amount.pk, N, number_min=Decimal('5'), number_max=Decimal('100.25')),
        ), 'in the order of the fields, whatever the order of the parameters')
        self.assertTrue(f.is_active)

    def test_several_options_and_none(self):
        f = self.parse({f'f_{self.priority.pk}': [str(self.low.pk), 'none', str(self.high.pk)]})
        self.assertEqual(f.fields, (
            FieldFilter(self.priority.pk, S, option_ids=(self.high.pk, self.low.pk), none=True),
        ), 'options in their own order')
        only_none = self.parse({f'f_{self.priority.pk}': 'none'})
        self.assertEqual(only_none.fields, (FieldFilter(self.priority.pk, S, none=True),))

    def test_one_bound_is_enough(self):
        self.assertEqual(
            self.parse({f'f_{self.deadline.pk}_to': '2026-11-30'}).fields,
            (FieldFilter(self.deadline.pk, D, date_to=datetime.date(2026, 11, 30)),),
        )
        self.assertEqual(
            self.parse({f'f_{self.amount.pk}_min': '1 234,50'}).fields,
            (FieldFilter(self.amount.pk, N, number_min=Decimal('1234.5')),),
        )

    def test_garbage_is_dropped(self):
        other_board = create_board(
            code=fresh_code(), name='Другая', owner=self.owner, actor=self.owner, member_ids=[self.member.pk],
        )
        foreign = create_field(other_board, actor=self.owner, name='Чужое', kind=S, options=[('X', 'blue')])
        foreign_option = foreign.options.get()
        cases = {
            'unknown field': {'f_999999': '1'},
            'field of another board': {f'f_{foreign.pk}': str(foreign_option.pk)},
            'option of another field': {f'f_{self.priority.pk}': str(self.yes.pk)},
            'option of another board': {f'f_{self.priority.pk}': str(foreign_option.pk)},
            'not an option': {f'f_{self.priority.pk}': ['abc', '', '-1']},
            'bad date': {f'f_{self.deadline.pk}_from': '30.11.2026', f'f_{self.deadline.pk}_to': '2026-02-30'},
            'bad number': {f'f_{self.amount.pk}_min': 'много', f'f_{self.amount.pk}_max': '1e5'},
            'empty': {f'f_{self.order.pk}': '   ', f'f_{self.amount.pk}_min': ''},
            'a date bound on a number': {f'f_{self.amount.pk}_from': '2026-01-01'},
            'a value on a date': {f'f_{self.deadline.pk}': '2026-01-01'},
        }
        for name, params in cases.items():
            with self.subTest(name):
                filters = self.parse(params)
                self.assertEqual(filters, NO_FILTERS)
                self.assertEqual(filters.query, '')

    def test_a_wrong_value_drops_only_itself(self):
        f = self.parse({
            f'f_{self.priority.pk}': [str(self.high.pk), 'abc', str(self.yes.pk)],
            f'f_{self.deadline.pk}_from': 'вчера',
            f'f_{self.deadline.pk}_to': '2026-11-30',
        })
        self.assertEqual(f.fields, (
            FieldFilter(self.deadline.pk, D, date_to=datetime.date(2026, 11, 30)),
            FieldFilter(self.priority.pk, S, option_ids=(self.high.pk,)),
        ))

    def test_an_archived_field_is_dropped_and_an_archived_option_kept(self):
        archive_option(self.low, actor=self.owner)
        archive_field(self.order, actor=self.owner)
        f = self.parse({f'f_{self.order.pk}': '3-1579', f'f_{self.priority.pk}': str(self.low.pk)})
        self.assertEqual(f.fields, (FieldFilter(self.priority.pk, S, option_ids=(self.low.pk,)),))

    def test_without_the_fields_the_parameters_are_not_read(self):
        f = parse_board_filters({f'f_{self.priority.pk}': str(self.high.pk), 'mine': '1'})
        self.assertEqual(f, BoardFilters(mine=True))

    def test_query_is_stable_and_reads_back(self):
        params = {
            f'f_{self.amount.pk}_max': '100,50',
            f'f_{self.priority.pk}': [str(self.low.pk), 'none', str(self.high.pk)],
            'q': 'план',
            f'f_{self.order.pk}': 'Заказ 7',
            f'f_{self.deadline.pk}_from': '2026-11-01',
            'mine': '1',
        }
        f = self.parse(params)
        self.assertEqual(
            f.query,
            f'mine=1&q=%D0%BF%D0%BB%D0%B0%D0%BD'
            f'&f_{self.order.pk}=%D0%97%D0%B0%D0%BA%D0%B0%D0%B7+7'
            f'&f_{self.deadline.pk}_from=2026-11-01'
            f'&f_{self.priority.pk}={self.high.pk}&f_{self.priority.pk}={self.low.pk}&f_{self.priority.pk}=none'
            f'&f_{self.amount.pk}_max=100.5',
        )
        again = parse_board_filters(QueryDict(f.query), board_fields(self.board))
        self.assertEqual(again, f)
        self.assertEqual(again.query, f.query)

    def test_dropping_a_field(self):
        f = self.parse({f'f_{self.priority.pk}': str(self.high.pk), f'f_{self.order.pk}': '3', 'mine': '1'})
        rest = f.without_field(self.priority.pk)
        self.assertEqual(rest, BoardFilters(mine=True, fields=(FieldFilter(self.order.pk, T, text='3'),)))
        self.assertIsNone(rest.field_filter(self.priority.pk))


# --------------------------------------------------------------------------
# Where a filter applies
# --------------------------------------------------------------------------


class FieldFilterApplyTests(FieldFilterMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def test_lists(self):
        key = f'f_{self.priority.pk}'
        self.assertEqual(self.titles({key: str(self.high.pk)}), ['Alpha', 'Delta'])
        self.assertEqual(self.titles({key: [str(self.high.pk), str(self.middle.pk)]}), ['Alpha', 'Beta', 'Delta'])
        self.assertEqual(self.titles({key: 'none'}), ['Gamma'])
        self.assertEqual(self.titles({key: [str(self.low.pk), 'none']}), ['Epsilon', 'Gamma'])

    def test_an_archived_option_still_filters(self):
        archive_option(self.low, actor=self.owner)
        self.assertEqual(self.titles({f'f_{self.priority.pk}': str(self.low.pk)}), ['Epsilon'])

    def test_dates_are_inclusive(self):
        key = f'f_{self.deadline.pk}'
        self.assertEqual(self.titles({f'{key}_to': '2026-11-30'}), ['Alpha', 'Gamma'])
        self.assertEqual(self.titles({f'{key}_from': '2026-11-30'}), ['Alpha', 'Beta'])
        self.assertEqual(self.titles({f'{key}_from': '2026-11-30', f'{key}_to': '2026-11-30'}), ['Alpha'])

    def test_numbers_are_inclusive_and_take_a_comma(self):
        key = f'f_{self.amount.pk}'
        self.assertEqual(self.titles({f'{key}_min': '10'}), ['Alpha', 'Beta'])
        self.assertEqual(self.titles({f'{key}_max': '10'}), ['Beta', 'Delta'])
        self.assertEqual(self.titles({f'{key}_min': '1234,5', f'{key}_max': '1 234,50'}), ['Alpha'])

    def test_texts_are_a_substring(self):
        key = f'f_{self.order.pk}'
        self.assertEqual(self.titles({key: '3-15'}), ['Alpha', 'Beta'])
        self.assertEqual(self.titles({key: '1579'}), ['Alpha'])

    def test_fields_and_the_old_filters_are_all_and(self):
        stop = f'f_{self.stop.pk}'
        deadline = f'f_{self.deadline.pk}_to'
        self.assertEqual(self.titles({stop: str(self.yes.pk)}), ['Alpha', 'Gamma'])
        self.assertEqual(self.titles({stop: str(self.yes.pk), deadline: '2026-11-15'}), ['Gamma'])
        high = {f'f_{self.priority.pk}': str(self.high.pk)}
        self.assertEqual(self.titles({**high, 'mine': '1'}), ['Alpha', 'Delta'])
        self.assertEqual(self.titles({stop: str(self.yes.pk), 'overdue': '1'}), ['Gamma'])
        self.assertEqual(self.titles({**high, 'q': 'Del'}), ['Delta'])
        self.assertEqual(self.titles({**high, 'q': 'Gam'}), [])

    def test_the_closing_column_takes_the_field_filter_but_not_overdue(self):
        done = str(done_column_of(self.board).pk)
        high = {f'f_{self.priority.pk}': str(self.high.pk)}
        self.assertEqual(counts(self.state(high))[done], 1)
        self.assertEqual(counts(self.state({f'f_{self.priority.pk}': str(self.middle.pk)}))[done], 0)
        # «Просроченные» still leaves «Готово» alone.
        self.assertEqual(counts(self.state({**high, 'overdue': '1'}))[done], 1)

    def test_counts_are_the_filtered_numbers_and_column_counts_agree(self):
        for params in (
            {f'f_{self.priority.pk}': [str(self.high.pk), str(self.low.pk)]},
            {f'f_{self.stop.pk}': 'none', 'mine': '1'},
            {f'f_{self.amount.pk}_min': '5', 'overdue': '1'},
            {},
        ):
            with self.subTest(params=params):
                state = self.state(params)
                self.assertEqual(column_counts(self.main, self.member, self.parse(params)), counts(state))
        high = counts(self.state({f'f_{self.priority.pk}': str(self.high.pk)}))
        self.assertEqual(high[str(column_of(self.board, 'TODO').pk)], 1)
        self.assertEqual(high[str(column_of(self.board, 'IN_PROGRESS').pk)], 0)

    def test_the_open_card_is_found_whatever_the_filter(self):
        state = build_board_state(
            self.board, self.main, self.member, card_id=self.beta.pk,
            filters=self.parse({f'f_{self.priority.pk}': str(self.high.pk)}),
        )
        self.assertEqual(state['card']['card'], self.beta)
        self.assertNotIn('Beta', visible(state))
        self.assertEqual([value['text'] for value in state['card']['field_values']][:1], ['3-1580'])

    def test_the_board_search_finds_by_a_text_value(self):
        self.assertEqual(self.titles({'q': '3-1579'}), ['Alpha'])
        self.assertEqual(self.titles({'q': '7-000'}), ['Delta'])
        # Not by a number, a date or an option: those are the field filters'.
        self.assertEqual(self.titles({'q': '1234'}), [])
        self.assertEqual(self.titles({'q': 'Высокий'}), [])
        archive_field(self.order, actor=self.owner)
        self.assertEqual(self.titles({'q': '3-1579'}), [], 'an archived field is not searched')


class FieldFilterPageTests(FieldFilterMixin, TestCase):
    def setUp(self):
        self.make_cards()
        self.client.force_login(self.member)
        self.high_query = {f'f_{self.priority.pk}': str(self.high.pk)}
        self.encoded = f'f_{self.priority.pk}={self.high.pk}'

    def test_page_and_fragment_agree_under_field_filters(self):
        for query in (
            {**self.high_query, 'card': self.alpha.pk},
            {**self.high_query, 'card': self.beta.pk, 'edit': '1'},
            {f'f_{self.deadline.pk}_to': '2026-11-30', f'f_{self.stop.pk}': str(self.yes.pk), 'card': self.gamma.pk},
            {f'f_{self.amount.pk}_min': '5', 'mine': '1'},
        ):
            with self.subTest(query=query):
                page = self.client.get(board_url(self.board), query).content.decode()
                fragment = self.client.get(fragment_url(self.board), query).json()
                if 'card' in query:
                    assert_page_matches_fragment(self, page, fragment)
                else:
                    assert_page_matches_fragment(
                        self, page, fragment, {'tabs': ('tabs_html',), 'columns': ('columns_html',)},
                    )

    def test_the_fragment_filters_and_carries_the_filter(self):
        fragment = self.client.get(fragment_url(self.board), {**self.high_query, 'card': self.beta.pk}).json()
        self.assertIn('title="Alpha"', fragment['columns_html'])
        self.assertNotIn('title="Beta"', fragment['columns_html'])
        self.assertIn('title="Delta"', fragment['columns_html'], 'the closing column too')
        self.assertTrue(fragment['fragment_url'].endswith(f'&{self.encoded}'))
        self.assertTrue(fragment['page_url'].endswith(f'&{self.encoded}'))
        self.assertEqual(fragment['card_id'], self.beta.pk, 'the open card, filtered out or not')

    def test_every_address_keeps_the_field_filter(self):
        content = main_of(self.client.get(board_url(self.board), {**self.high_query, 'card': self.alpha.pk}))
        page = board_url(self.board)
        enc = self.encoded
        self.assertIn(f'href="{page}?card={self.alpha.pk}&amp;{enc}"', content, 'плитка')
        self.assertIn(f'href="{page}?{enc}" data-board-drawer-close', content, '«×»')
        self.assertIn(
            f'data-card-move-url="{reverse("boards:card_move", args=[self.board.pk, self.alpha.pk])}?{enc}"',
            content,
        )
        self.assertIn(f'action="{reverse("boards:card_complete", args=[self.board.pk, self.alpha.pk])}?{enc}"', content)
        self.assertEqual(
            attribute(content, 'data-board-page-url').replace('&amp;', '&'),
            f'{page}?card={self.alpha.pk}&tab=description&{enc}',
        )
        self.assertIn(f'href="{page}?card={self.alpha.pk}" data-board-filter-reset>Сбросить</a>', content)

    def test_a_redirect_after_a_post_keeps_the_field_filter(self):
        url = reverse('boards:card_move', args=[self.board.pk, self.alpha.pk])
        response = self.client.post(f'{url}?{self.encoded}', {'column_id': column_of(self.board, 'REVIEW').pk})
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.alpha.pk}&{self.encoded}', fetch_redirect_response=False,
        )
        comment = reverse('boards:card_comment', args=[self.board.pk, self.alpha.pk])
        response = self.client.post(f'{comment}?{self.encoded}&f_999=x', {'text': 'Привет'})
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.alpha.pk}&tab=chat&{self.encoded}',
            fetch_redirect_response=False,
        )

    def test_drag_json_counts_follow_the_field_filter(self):
        url = reverse('boards:card_move', args=[self.board.pk, self.alpha.pk])
        answer = self.client.post(
            f'{url}?{self.encoded}', {'column_id': column_of(self.board, 'IN_PROGRESS').pk},
            HTTP_X_REQUESTED_WITH='fetch',
        ).json()
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['counts'], {
            str(column_of(self.board, 'TODO').pk): 0,
            str(column_of(self.board, 'IN_PROGRESS').pk): 1,
            str(column_of(self.board, 'REVIEW').pk): 0,
            str(done_column_of(self.board).pk): 1,
        })

    def test_the_fields_panel_and_the_chips(self):
        archive_option(self.low, actor=self.owner)
        archive_field(self.customer, actor=self.owner)
        content = main_of(self.client.get(board_url(self.board), {
            **self.high_query, f'f_{self.deadline.pk}_to': '2026-11-30', 'card': self.alpha.pk,
        }))
        self.assertIn('Поля · 2', content)
        # A row per live field, the archived one gone.
        for name in ('Номер заявки', 'Срок изг.', 'Приоритет', 'Стоп', 'Сумма'):
            self.assertIn(f'<legend class="board-menu__label user-text">{name}</legend>', content)
        self.assertNotIn('<legend class="board-menu__label user-text">Заказ покупателя</legend>', content)
        # The ticked option, the archived one not offered while not chosen, «не задано».
        self.assertIn(f'name="f_{self.priority.pk}" value="{self.high.pk}" checked', content)
        self.assertNotIn(f'name="f_{self.priority.pk}" value="{self.low.pk}"', content)
        self.assertIn(f'name="f_{self.priority.pk}" value="none"', content)
        self.assertIn(f'name="f_{self.deadline.pk}_to" value="2026-11-30"', content)
        self.assertIn(f'name="f_{self.amount.pk}_min" value="" aria-label="Сумма: от" autocomplete="off" data-registry-delayed', content)
        self.assertIn('board-chip board-chip--red', content)
        # The chips, each «×» the address without that field.
        self.assertIn('<span class="user-text">Приоритет: Высокий</span>', content)
        self.assertIn('<span class="user-text">Срок изг.: по 30.11.2026</span>', content)
        page = board_url(self.board)
        self.assertIn(
            f'href="{page}?card={self.alpha.pk}&amp;tab=description&amp;f_{self.deadline.pk}_to=2026-11-30" data-board-filter-chip',
            content,
        )
        self.assertIn(
            f'href="{page}?card={self.alpha.pk}&amp;tab=description&amp;{self.encoded}" data-board-filter-chip',
            content,
        )

    def test_an_archived_option_is_offered_while_chosen(self):
        archive_option(self.low, actor=self.owner)
        content = main_of(self.client.get(board_url(self.board), {f'f_{self.priority.pk}': str(self.low.pk)}))
        self.assertIn(f'name="f_{self.priority.pk}" value="{self.low.pk}" checked', content)
        self.assertIn('<span class="user-text">Приоритет: Низкий</span>', content)
        self.assertIn('Epsilon', content)

    def test_chip_wording(self):
        content = main_of(self.client.get(board_url(self.board), {
            f'f_{self.priority.pk}': [str(self.high.pk), str(self.middle.pk), 'none'],
            f'f_{self.deadline.pk}_from': '2026-11-01', f'f_{self.deadline.pk}_to': '2026-11-30',
            f'f_{self.amount.pk}_min': '1234,5',
            f'f_{self.order.pk}': '3-1579',
        }))
        self.assertIn('Поля · 4', content)
        for chip in (
            'Приоритет: Высокий, Средний, не задано',
            'Срок изг.: с 01.11.2026 по 30.11.2026',
            'Сумма: от 1 234,5',
            'Номер заявки: «3-1579»',
        ):
            self.assertIn(f'<span class="user-text">{chip}</span>', content)
        self.assertIn(f'name="f_{self.amount.pk}_min" value="1234,5"', content)

    def test_no_fields_panel_without_live_fields(self):
        for field in (self.order, self.customer, self.deadline, self.priority, self.stop, self.amount):
            archive_field(field, actor=self.owner)
        content = main_of(self.client.get(board_url(self.board)))
        self.assertNotIn('board-filters__fields', content)
        self.assertNotIn('Поля ·', content)

    def test_no_chips_and_no_count_without_a_field_filter(self):
        content = main_of(self.client.get(board_url(self.board), {'mine': '1'}))
        self.assertIn('>Поля<', content.replace('</summary>', '<'))
        self.assertNotIn('board-filter-chips', content)

    def test_wrong_parameters_are_never_an_error(self):
        for query in (
            {'f_abc': '1'}, {'f_': ''}, {'f_99999999999999999999': '1'},
            {f'f_{self.priority.pk}': '9' * 30}, {f'f_{self.amount.pk}_min': '9' * 40},
            {f'f_{self.deadline.pk}_from': '9999-99-99'}, {f'f_{self.order.pk}': 'x' * 5000},
        ):
            with self.subTest(query=query):
                page = self.client.get(board_url(self.board), query)
                self.assertEqual(page.status_code, 200)
                fragment = self.client.get(fragment_url(self.board), query)
                self.assertEqual(fragment.status_code, 200)
                move = reverse('boards:card_move', args=[self.board.pk, self.alpha.pk])
                answer = self.client.post(
                    f'{move}?{urlencode(query)}',
                    {'column_id': column_of(self.board, 'TODO').pk}, HTTP_X_REQUESTED_WITH='fetch',
                )
                self.assertEqual(answer.status_code, 200)

    def test_a_field_archived_while_the_address_is_open(self):
        query = {f'f_{self.order.pk}': '3-1579'}
        before = self.client.get(fragment_url(self.board), query).json()
        self.assertNotIn('title="Beta"', before['columns_html'])
        archive_field(self.order, actor=self.owner)
        after = self.client.get(fragment_url(self.board), query)
        self.assertEqual(after.status_code, 200)
        self.assertIn('title="Beta"', after.json()['columns_html'], 'the filter is dropped')
        self.assertEqual(after.json()['reset_url'], board_url(self.board))
        page = self.client.get(board_url(self.board), query)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('board-filter-chips', main_of(page))


# --------------------------------------------------------------------------
# Finding a card by a value: the board, «Задачи», the topbar
# --------------------------------------------------------------------------


class FieldValueSearchTests(FieldFilterMixin, TestCase):
    def setUp(self):
        self.make_cards()

    def registry(self, user, term):
        self.client.force_login(user)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': term})
        return [row['task'].board_card_id for row in response.context['rows']]

    def test_registry_finds_by_order_number(self):
        self.assertEqual(self.registry(self.member, '3-1579'), [self.alpha.pk])
        self.assertEqual(sorted(self.registry(self.member, '3-15')), sorted([self.alpha.pk, self.beta.pk]))

    def test_quick_search_finds_by_order_number(self):
        hits = dict(quick_search(self.member, '3-1579'))['Задачи']
        self.assertEqual([hit.url for hit in hits], [reverse('tasks:detail', args=[task_of(self.alpha).pk])])

    def test_an_archived_field_is_not_searched(self):
        archive_field(self.order, actor=self.owner)
        self.assertEqual(self.registry(self.member, '3-1579'), [])
        self.assertEqual(dict(quick_search(self.member, '3-1579')).get('Задачи', []), [])

    def test_only_text_fields_are_searched(self):
        self.assertEqual(self.registry(self.member, '1234'), [])
        self.assertEqual(dict(quick_search(self.member, 'Высокий')).get('Задачи', []), [])


class ForeignFieldValueSearchTests(AccessFixture, TestCase):
    """With the real access rule: a value on a board one is not on finds nothing."""

    def setUp(self):
        super().setUp()
        field = create_field(self.foreign, actor=self.admin, name='Номер заявки', kind=T)
        own = create_field(self.board, actor=self.admin, name='Номер заявки', kind=T)
        self.foreign_card = new_card(
            self.foreign, self.admin, 'Заказ ПДО', assignees=[self.pdo], field_values={field.pk: '3-1579'},
        )
        self.own_card = new_card(
            self.board, self.admin, 'Заказ ОТК', assignees=[self.otk], field_values={own.pk: '5-2468'},
        )

    def test_not_in_the_registry_the_topbar_or_the_board(self):
        self.assertEqual(dict(quick_search(self.otk, '3-1579')).get('Задачи', []), [])
        self.assertTrue(dict(quick_search(self.pdo, '3-1579'))['Задачи'])
        self.assertTrue(dict(quick_search(self.otk, '5-2468'))['Задачи'])
        self.client.force_login(self.otk)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': '3-1579'})
        self.assertEqual(list(response.context['rows']), [])
        mine = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': '5-2468'})
        self.assertEqual([row['task'].board_card_id for row in mine.context['rows']], [self.own_card.pk])
        html = self.client.get(fragment_url(self.board), {'q': '3-1579'}).json()['columns_html']
        self.assertNotIn('Заказ', html)


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


class FieldFilterQueryCountTests(FieldFilterMixin, TestCase):
    """The page and its fragment cost the same with 0, 1 and 5 field filters,
    and «Задачи» and the topbar search the same however many cards a value
    finds."""

    def setUp(self):
        self.make_fields()
        # A card every filter below keeps, open and completed, so no prefetch
        # is skipped for an empty list — and one no filter keeps.
        values = {
            self.order.pk: '3-1579', self.deadline.pk: '2026-11-30', self.priority.pk: self.high.pk,
            self.stop.pk: self.yes.pk, self.amount.pk: '12',
        }
        self.open_card = self.card('Открытая', field_values=values)
        done = self.card('Завершённая', field_values=values)
        complete_task(task_of(done), self.member, 'Готово')
        self.card('Другая', field_values={self.priority.pk: self.low.pk})
        self.client.force_login(self.member)
        self.filters = [
            {f'f_{self.priority.pk}': [str(self.high.pk), str(self.middle.pk)]},
            {f'f_{self.order.pk}': '1579'},
            {f'f_{self.deadline.pk}_from': '2026-11-01', f'f_{self.deadline.pk}_to': '2026-12-31'},
            {f'f_{self.stop.pk}': [str(self.yes.pk), 'none']},
            {f'f_{self.amount.pk}_min': '10', f'f_{self.amount.pk}_max': '12,5'},
        ]

    def count(self, url, query):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(url, query)
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def counts(self, active):
        query = {'card': self.open_card.pk}
        for item in self.filters[:active]:
            query.update(item)
        return (self.count(board_url(self.board), query), self.count(fragment_url(self.board), query))

    def test_the_count_does_not_grow_with_the_filters(self):
        baseline = self.counts(0)
        self.assertEqual(baseline[0], PAGE_QUERIES)
        self.assertEqual(self.counts(1), baseline)
        self.assertEqual(self.counts(5), baseline)
        page = main_of(self.client.get(board_url(self.board), {
            'card': self.open_card.pk, **{k: v for item in self.filters for k, v in item.items()},
        }))
        self.assertIn('Поля · 5', page)
        self.assertIn('title="Открытая"', page)
        self.assertNotIn('title="Другая"', page)

    def test_registry_and_topbar_searches_do_not_grow(self):
        def registry():
            with CaptureQueriesContext(connection) as queries:
                response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': '3-15'})
            return len(queries), len(response.context['rows'])

        def topbar():
            user = type(self.member).objects.get(pk=self.member.pk)
            with CaptureQueriesContext(connection) as queries:
                hits = dict(quick_search(user, '3-15')).get('Задачи', [])
            return len(queries), len(hits)

        registry_before, found = registry()
        self.assertEqual(found, 1)
        topbar_before, hits = topbar()
        self.assertEqual(hits, 2)
        for index in range(5):
            self.card(f'Ещё {index}', field_values={self.order.pk: f'3-15{index}'})
        registry_after, found = registry()
        self.assertEqual(found, 6)
        topbar_after, hits = topbar()
        self.assertEqual(hits, 7)
        self.assertEqual(registry_after, registry_before)
        self.assertEqual(topbar_after, topbar_before)

