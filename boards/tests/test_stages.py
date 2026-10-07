"""«Норматив этапа» (stage 23): the traffic light, «Просрочен этап», «Этапы»
on «Описание», the stage columns of «Таблица» and the «Этапы» tab of
«Отклонения».

The light's boundaries are checked on fixed dates through the pure helpers
(`stage_plan()`, `stage_light()`, `late_cutoff()`), so the tests do not
depend on today's weekday; what reads «today» on a page is checked with ages
far enough from any boundary.
"""

import datetime

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from ecosystem.workdays import add_working_days, working_days_between

from ..models import BoardCardEvent, BoardColumn
from ..selectors import (
    BoardFilters,
    build_board_state,
    build_board_table,
    build_stage_report,
    late_cutoff,
    stage_light,
    stage_plan,
)
from ..services import (
    complete_card,
    create_sub_board,
    move_card,
    reopen_card,
    set_column_norm,
)
from .helpers import BoardFixtureMixin, board_url, make_user
from .test_lifecycle import main_of
from .test_table import read_xlsx


def noon(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(12, 0)))


def days_ago(days):
    return timezone.localdate() - datetime.timedelta(days=days)


MONDAY = datetime.date(2026, 10, 5)
THURSDAY = datetime.date(2026, 10, 8)
FRIDAY = datetime.date(2026, 10, 9)
NEXT_MONDAY = datetime.date(2026, 10, 12)
NEXT_TUESDAY = datetime.date(2026, 10, 13)


class LightTests(SimpleTestCase):
    def test_the_plan_is_the_norm_in_working_days_from_the_day_entered(self):
        self.assertEqual(stage_plan(noon(THURSDAY), 2), NEXT_MONDAY)
        self.assertEqual(stage_plan(noon(MONDAY), 3), THURSDAY)
        self.assertIsNone(stage_plan(noon(MONDAY), None))
        self.assertIsNone(stage_plan(None, 3))

    def test_three_colours_and_their_boundaries(self):
        # Green: more than one working day left.
        self.assertEqual(stage_light(THURSDAY, today=MONDAY), 'green')
        # Yellow: the next working day, and today.
        self.assertEqual(stage_light(datetime.date(2026, 10, 6), today=MONDAY), 'yellow')
        self.assertEqual(stage_light(MONDAY, today=MONDAY), 'yellow')
        # Friday → Monday is one working day: yellow, not green.
        self.assertEqual(stage_light(NEXT_MONDAY, today=FRIDAY), 'yellow')
        self.assertEqual(stage_light(NEXT_TUESDAY, today=FRIDAY), 'green')
        # Red: the plan is past, by one day or by a weekend.
        self.assertEqual(stage_light(THURSDAY, today=FRIDAY), 'red')
        self.assertEqual(stage_light(FRIDAY, today=NEXT_MONDAY), 'red')
        # No norm, no plan, no colour.
        self.assertIsNone(stage_light(None, today=MONDAY))

    def test_the_late_cutoff_agrees_with_the_plan(self):
        for today in (MONDAY, THURSDAY, FRIDAY, NEXT_MONDAY):
            for norm in (1, 2, 3, 5, 10):
                cutoff = late_cutoff(norm, today)
                with self.subTest(today=today, norm=norm):
                    self.assertGreaterEqual(add_working_days(cutoff, norm), today)
                    self.assertLess(add_working_days(cutoff - datetime.timedelta(days=1), norm), today)


class StageFixture(BoardFixtureMixin):
    def setUp(self):
        self.todo = self.column('TODO')
        self.work = self.column('IN_PROGRESS')
        set_column_norm(self.todo, actor=self.owner, days=3)

    def enter(self, card, day, kinds=('CREATED', 'MOVED', 'REOPENED')):
        BoardCardEvent.objects.filter(card=card, kind__in=kinds).update(created_at=noon(day))

    def tiles(self, **filters):
        state = build_board_state(self.board, self.main, self.member, filters=BoardFilters(**filters))
        return {item['card'].pk: item for column in state['columns'] for item in column['cards']}


class TileLightTests(StageFixture, TestCase):
    def test_green_red_and_none_on_the_tiles(self):
        fresh = self.card('Свежая')
        late = self.card('Опоздала')
        self.enter(late, days_ago(9))
        unwatched = self.card('Без норматива', stage='IN_PROGRESS')
        self.enter(unwatched, days_ago(30))
        tiles = self.tiles()
        self.assertEqual(tiles[fresh.pk]['light'], 'green')
        self.assertEqual(tiles[fresh.pk]['plan_exit'], add_working_days(timezone.localdate(), 3))
        self.assertEqual(tiles[late.pk]['light'], 'red')
        self.assertTrue(tiles[late.pk]['is_late'])
        self.assertEqual(
            tiles[late.pk]['stage_deviation'],
            working_days_between(add_working_days(days_ago(9), 3), timezone.localdate()),
        )
        self.assertIsNone(tiles[unwatched.pk]['light'])
        self.client.force_login(self.member)
        page = main_of(self.client.get(board_url(self.board)))
        self.assertEqual(page.count('board-tile--late'), 1)
        self.assertIn('board-light board-light--green', page)
        self.assertIn('board-light board-light--red', page)
        plan = add_working_days(timezone.localdate(), 3)
        self.assertIn(f'план до {plan:%d.%m}', page)
        self.assertIn(f'план выхода {plan:%d.%m.%Y}', page)

    def test_yellow_when_the_plan_is_today(self):
        card = self.card('Сегодня выходит')
        set_column_norm(self.todo, actor=self.owner, days=1)
        # Entered on the working day before today: the plan is today.
        today = timezone.localdate()
        entered = today - datetime.timedelta(days=1)
        while add_working_days(entered, 1) != today:
            entered -= datetime.timedelta(days=1)
            if (today - entered).days > 7:  # today is a weekend: no such day
                self.skipTest('today is not a working day')
        self.enter(card, entered)
        self.assertEqual(self.tiles()[card.pk]['light'], 'yellow')

    def test_a_closed_card_has_no_light(self):
        card = self.card('Готово')
        self.enter(card, days_ago(20))
        complete_card(card, actor=self.member, execution_comment='Да')
        item = self.tiles()[card.pk]
        self.assertIsNone(item['light'])
        self.assertFalse(item['is_late'])

    def test_the_stale_filter_keeps_the_red_ones(self):
        fresh = self.card('Свежая')
        late = self.card('Опоздала')
        self.enter(late, days_ago(9))
        shown = self.tiles(stale=True)
        self.assertIn(late.pk, shown)
        self.assertNotIn(fresh.pk, shown)
        self.client.force_login(self.member)
        page = main_of(self.client.get(board_url(self.board), {'stale': '1'}))
        self.assertIn('Просрочен этап', page)
        self.assertIn('Показать · 1', page)

    def test_the_query_count_does_not_grow_with_the_lights(self):
        self.client.force_login(self.member)

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(board_url(self.board))
            return len(queries)

        self.card('Одна')
        baseline = count()
        for index in range(6):
            card = self.card(f'Ещё {index}')
            self.enter(card, days_ago(index * 3))
        self.assertEqual(count(), baseline)


class StagePathTests(StageFixture, TestCase):
    def stages(self, card, user=None):
        state = build_board_state(self.board, card.sub_board, user or self.member, card_id=card.pk)
        return state['card']['stages']

    def test_the_path_through_the_columns(self):
        card = self.card('Заказ')
        self.enter(card, MONDAY, kinds=('CREATED',))
        move_card(card, actor=self.member, column=self.work)
        BoardCardEvent.objects.filter(card=card, kind='MOVED').update(created_at=noon(FRIDAY))
        rows = self.stages(card)
        self.assertEqual([row['column'] for row in rows], [self.todo.name, self.work.name])
        first, second = rows
        self.assertEqual((first['entered'], first['exited']), (MONDAY, FRIDAY))
        # Norm 3 from Monday: Thursday; left on Friday — one working day late.
        self.assertEqual((first['plan'], first['days'], first['deviation']), (THURSDAY, 4, 1))
        # «В работе» has no norm: no plan, no deviation; it is the current stay.
        self.assertTrue(second['is_current'])
        self.assertIsNone(second['plan'])
        self.assertIsNone(second['deviation'])

    def test_the_current_stage_shows_what_is_left_until_it_is_late(self):
        from types import SimpleNamespace

        from ..selectors import card_stages

        card = self.card('Заказ')
        events = [SimpleNamespace(
            kind='CREATED', details={'column_id': self.todo.pk, 'column': self.todo.name},
            created_at=noon(MONDAY),
        )]
        self.todo.refresh_from_db()
        columns = {self.todo.pk: self.todo}
        # Norm 3 from Monday: the plan is Thursday.
        for today, remaining, deviation in (
            (datetime.date(2026, 10, 6), 2, None),   # Tuesday: «осталось 2 р.д.»
            (THURSDAY, 0, None),                      # «план сегодня»
            (FRIDAY, None, 1),                        # late: «+1»
        ):
            with self.subTest(today=today):
                row = card_stages(card, events, columns, today=today)[0]
                self.assertEqual((row['remaining'], row['deviation']), (remaining, deviation))
        self.client.force_login(self.member)
        self.enter(card, timezone.localdate(), kinds=('CREATED',))
        html = self.client.get(
            reverse('boards:fragment', args=[self.board.pk, self.main.pk]), {'card': card.pk},
        ).json()['stages_html']
        self.assertIn('board-stages__left', html)
        self.assertIn('осталось 3 р.д.', html)

    def test_completed_reopened_and_moved_between_sub_boards(self):
        card = self.card('Заказ')
        complete_card(card, actor=self.member, execution_comment='Да')
        reopen_card(card, actor=self.admin)
        other_tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        target = BoardColumn.objects.filter(sub_board=other_tab, is_done=False).order_by('position').first()
        move_card(card, actor=self.member, column=target)
        card.refresh_from_db()
        rows = self.stages(card)
        self.assertEqual(
            [(row['column'], row['is_done'], row['is_current']) for row in rows],
            [
                (self.todo.name, False, False),
                ('Готово', True, False),
                (self.todo.name, False, False),
                (target.name, False, True),
            ],
        )

    def test_the_block_is_live_its_own_and_raises_no_banner(self):
        card = self.card('Заказ')
        self.client.force_login(self.member)
        url = reverse('boards:fragment', args=[self.board.pk, self.main.pk])
        before = self.client.get(url, {'card': card.pk}).json()
        page = main_of(self.client.get(board_url(self.board), {'card': card.pk}))
        self.assertIn('data-live-board-stages', page)
        self.assertIn(f'data-stages-revision="{before["stages_revision"]}"', page)
        self.assertIn('по текущему нормативу', before['stages_html'])
        move_card(card, actor=self.colleague, column=self.work)
        after = self.client.get(url, {'card': card.pk}).json()
        self.assertNotEqual(after['stages_revision'], before['stages_revision'])

    def test_a_subtask_has_no_stages(self):
        from ..services import create_subtask

        card = self.card('Заказ')
        sub = create_subtask(card, actor=self.member, title='Позиция')
        self.assertEqual(self.stages(sub), [])


class TableStageTests(StageFixture, TestCase):
    def test_plan_and_deviation_columns_and_their_sort(self):
        fresh = self.card('Свежая')
        late = self.card('Опоздала')
        self.enter(late, days_ago(12))
        state = build_board_table(self.board, self.main, self.member, sort='-stage')
        codes = [row['card'].pk for row in state['rows']]
        self.assertEqual(codes[:2], [late.pk, fresh.pk])
        late_row = state['rows'][0]
        self.assertEqual(late_row['plan_exit'], add_working_days(days_ago(12), 3))
        self.assertGreater(late_row['stage_deviation'], 0)
        self.client.force_login(self.member)
        rows, _ = read_xlsx(self.client.get(board_url(self.board), {'view': 'table', 'export': 'xlsx'}).content)
        header = [cell[1] for cell in rows[0]]
        plan, deviation = header.index('План выхода из этапа'), header.index('Отклонение этапа, р.д.')
        by_code = {row[0][1]: row for row in rows[1:]}
        self.assertEqual(by_code[late.code][plan], ('d', late_row['plan_exit']))
        self.assertEqual(by_code[late.code][deviation][0], 'n')
        page = main_of(self.client.get(board_url(self.board), {'view': 'table', 'sort': '-stage'}))
        self.assertIn('Отклонение этапа, р.д.', page)


class StageReportTests(StageFixture, TestCase):
    """Three cards leave «Сделать» (norm 3) in early September after 2, 3 and
    6 working days; one left it in August (outside the period)."""

    def setUp(self):
        super().setUp()
        self.period = (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30))
        stays = (
            (datetime.date(2026, 9, 1), datetime.date(2026, 9, 3)),   # Tue → Thu: 2
            (datetime.date(2026, 9, 1), datetime.date(2026, 9, 4)),   # Tue → Fri: 3
            (datetime.date(2026, 9, 1), datetime.date(2026, 9, 9)),   # Tue → Wed: 6
            (datetime.date(2026, 8, 3), datetime.date(2026, 8, 5)),   # August
        )
        for index, (entered, left) in enumerate(stays):
            card = self.card(f'Карточка {index}')
            self.enter(card, entered, kinds=('CREATED',))
            move_card(card, actor=self.member, column=self.work)
            BoardCardEvent.objects.filter(card=card, kind='MOVED').update(created_at=noon(left))
        self.late = self.card('Стоит давно')
        self.enter(self.late, days_ago(15))

    def report(self, **kwargs):
        return build_stage_report(
            self.board, date_from=self.period[0], date_to=self.period[1], **kwargs,
        )

    def test_average_longest_share_and_red_now(self):
        rows = {row['column'].pk: row for row in self.report()['stage_rows']}
        todo = rows[self.todo.pk]
        self.assertEqual((todo['exits'], todo['average'], todo['longest']), (3, 3.7, 6))
        self.assertEqual((todo['within'], todo['within_share']), (2, 67))
        self.assertEqual(todo['late_now'], 1)
        work = rows[self.work.pk]
        self.assertEqual((work['exits'], work['norm'], work['within_share']), (0, None, None))

    def test_the_period_decides_which_exits_count(self):
        self.period = (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31))
        rows = {row['column'].pk: row for row in self.report()['stage_rows']}
        self.assertEqual((rows[self.todo.pk]['exits'], rows[self.todo.pk]['longest']), (1, 2))

    def test_the_page_its_excel_and_a_stranger(self):
        self.client.force_login(self.member)
        url = reverse('boards:deviations', args=[self.board.pk])
        params = {'view': 'stages', 'from': '2026-09-01', 'to': '2026-09-30'}
        page = main_of(self.client.get(url, params))
        self.assertIn('Сейчас просрочен этап', page)
        self.assertIn('67 % (2 из 3)', page)
        rows, _ = read_xlsx(self.client.get(url, {**params, 'export': 'xlsx'}).content)
        self.assertEqual([cell[1] for cell in rows[0]][:4], ['Поддоска', 'Колонка', 'Норматив, р.д.', 'Вышло карточек'])
        todo = next(row for row in rows[1:] if row[1][1] == self.todo.name)
        self.assertEqual(todo[3][1], 3)
        from unittest import mock

        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            self.client.force_login(make_user('stage_stranger'))
            self.assertEqual(self.client.get(url, params).status_code, 403)

    def test_three_queries_whatever_the_cards(self):
        with CaptureQueriesContext(connection) as before:
            self.report()
        for index in range(5):
            card = self.card(f'Ещё {index}')
            move_card(card, actor=self.member, column=self.work)
            BoardCardEvent.objects.filter(card=card, kind='MOVED').update(created_at=noon(datetime.date(2026, 9, 10)))
        with CaptureQueriesContext(connection) as after:
            self.report()
        self.assertEqual(len(after), len(before))
        self.assertEqual(len(before), 3)
