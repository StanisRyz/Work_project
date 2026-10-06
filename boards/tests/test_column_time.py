"""Stage 17: how long a card has stood in its column, and «Застой».

«В колонке с» is the latest `CREATED`/`MOVED`/`REOPENED` entry of the card's
journal, read by one subquery of the tasks' own query; days are calendar days
by the local date. A working column may carry a threshold
(`BoardColumn.stale_after_days`): a tile that has stood there that long is
highlighted, and «Застрявшие» (`?stale=1`) keeps exactly those — on the page,
in its fragment and in a drag's counts.
"""

import datetime
from contextlib import contextmanager
from unittest import mock

from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.services import complete_task

from ..models import STALE_DAYS_MAX, BoardCard, BoardCardEvent, BoardColumn
from ..selectors import (
    BoardFilters,
    build_board_state,
    column_counts,
    days_in_column,
    parse_board_filters,
)
from ..services import (
    BoardError,
    archive_board,
    complete_card,
    move_card,
    reopen_card,
    set_column_pins,
    set_column_stale_days,
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    column_of,
    done_column_of,
    fragment_url,
)
from .test_journal import task_of


def at_days_ago(days):
    """Noon of the local day `days` ago — far from any midnight."""
    day = timezone.localdate() - datetime.timedelta(days=days)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(12, 0)))


def age(card, kinds=('CREATED', 'MOVED', 'REOPENED'), days=0):
    """Put every journal entry of `card` of `kinds` `days` back."""
    BoardCardEvent.objects.filter(card=card, kind__in=kinds).update(created_at=at_days_ago(days))


def tile_items(state):
    return {item['card'].pk: item for column in state['columns'] for item in column['cards']}


def structure_events(publisher):
    return [
        event for event in publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)
        if event.data['change'] == 'structure_changed'
    ]


class ColumnTimeMixin(BoardFixtureMixin):
    @contextmanager
    def published(self):
        """The realtime events published by the commits inside the block."""
        with capture_realtime_events() as publisher, self.captureOnCommitCallbacks(execute=True):
            yield publisher

    def state(self, user=None, **filters):
        return build_board_state(
            self.board, self.main, user or self.member, filters=BoardFilters(**filters),
        )

    def item(self, card, user=None, **filters):
        return tile_items(self.state(user, **filters)).get(card.pk)


class DaysInColumnTests(TestCase):
    def test_today_is_zero_and_yesterday_one(self):
        self.assertEqual(days_in_column(timezone.now()), 0)
        self.assertEqual(days_in_column(at_days_ago(1)), 1)
        self.assertEqual(days_in_column(at_days_ago(9)), 9)
        self.assertIsNone(days_in_column(None))

    def test_calendar_days_by_the_local_date_not_by_hours(self):
        today = datetime.date(2026, 10, 6)
        just_before_midnight = timezone.make_aware(datetime.datetime(2026, 10, 5, 23, 59))
        just_after_midnight = timezone.make_aware(datetime.datetime(2026, 10, 6, 0, 1))
        self.assertEqual(days_in_column(just_before_midnight, today), 1)
        self.assertEqual(days_in_column(just_after_midnight, today), 0)
        # Weekends count: Friday → Monday is three days.
        friday = timezone.make_aware(datetime.datetime(2026, 10, 2, 10, 0))
        self.assertEqual(days_in_column(friday, today), 4)


class InColumnSinceTests(ColumnTimeMixin, TestCase):
    def test_a_new_card_has_stood_since_its_creation(self):
        card = self.card()
        created = BoardCardEvent.objects.get(card=card, kind='CREATED').created_at
        item = self.item(card)
        self.assertEqual(item['in_column_since'], created)
        self.assertEqual(item['in_column_days'], 0)

    def test_n_days_after_creation(self):
        card = self.card()
        age(card, days=4)
        self.assertEqual(self.item(card)['in_column_days'], 4)

    def test_a_move_to_another_column_restarts_the_clock(self):
        card = self.card()
        age(card, days=6)
        move_card(card, actor=self.member, column=self.column('IN_PROGRESS'))
        self.assertEqual(self.item(card)['in_column_days'], 0)
        age(card, kinds=('MOVED',), days=2)
        self.assertEqual(self.item(card)['in_column_days'], 2)

    def test_a_reorder_within_the_column_does_not(self):
        first = self.card('Первая')
        second = self.card('Вторая')
        age(first, days=5)
        age(second, days=5)
        move_card(second, actor=self.member, column=self.column('TODO'), before_card_id=first.pk)
        self.assertFalse(BoardCardEvent.objects.filter(card=second, kind='MOVED').exists())
        self.assertEqual(self.item(second)['in_column_days'], 5)

    def test_reopening_restarts_the_clock(self):
        card = self.card()
        complete_card(card, actor=self.member, execution_comment='Готово')
        age(card, days=7)
        self.assertIsNone(tile_items(self.state()).get(card.pk)['in_column_days'])
        reopen_card(card, actor=self.admin)
        age(card, kinds=('CREATED', 'MOVED'), days=7)
        self.assertEqual(self.item(card)['in_column_days'], 0)

    def test_the_pinned_people_of_a_move_do_not_restart_it(self):
        target = self.column('IN_PROGRESS')
        set_column_pins(target, actor=self.owner, user_ids=[self.colleague.pk], mode='ADD')
        card = self.card()
        move_card(card, actor=self.member, column=target)
        # The pins wrote an «Исполнители по колонке» entry after the move.
        self.assertEqual(
            list(BoardCardEvent.objects.filter(card=card).order_by('pk').values_list('kind', flat=True)),
            ['CREATED', 'MOVED', 'EDITED'],
        )
        age(card, kinds=('CREATED', 'MOVED'), days=3)
        self.assertEqual(self.item(card)['in_column_days'], 3)

    def test_a_card_without_any_entry_falls_back_to_its_creation(self):
        card = self.card()
        BoardCardEvent.objects.filter(card=card).delete()
        BoardCard.objects.filter(pk=card.pk).update(created_at=at_days_ago(2))
        self.assertEqual(self.item(card)['in_column_days'], 2)

    def test_completed_and_cancelled_cards_say_nothing(self):
        done = self.card('Сделано')
        complete_card(done, actor=self.member, execution_comment='Готово')
        state = self.state()
        done_item = tile_items(state)[done.pk]
        self.assertIsNone(done_item['in_column_days'])
        self.assertFalse(done_item['is_stale'])
        page = self.client_page()
        self.assertNotIn('в колонке', page.split('data-card-id="%d"' % done.pk, 1)[1].split('</li>', 1)[0])

    def client_page(self, **query):
        self.client.force_login(self.member)
        return self.client.get(board_url(self.board), query).content.decode()

    def test_the_tile_says_today_or_n_days(self):
        fresh = self.card('Свежая')
        old = self.card('Старая')
        age(old, days=3)
        page = self.client_page()
        self.assertIn('в колонке сегодня', page)
        self.assertIn('в колонке 3 дн.', page)
        self.assertIn(f'В колонке с {at_days_ago(3):%d.%m.%Y}', page)
        self.assertTrue(fresh)

    def test_the_query_count_does_not_grow_with_the_cards(self):
        self.card('Первая')
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as few:
            self.client.get(board_url(self.board))
        for index in range(6):
            card = self.card(f'Ещё {index}')
            age(card, days=index)
            if index % 2:
                move_card(card, actor=self.member, column=self.column('REVIEW'))
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(board_url(self.board))
        self.assertIn('в колонке 4 дн.', response.content.decode())
        self.assertEqual(len(many), len(few))


# --------------------------------------------------------------------------
# «Застой»: the threshold of a column
# --------------------------------------------------------------------------


class StaleServiceTests(ColumnTimeMixin, TestCase):
    def test_owner_and_administrator_set_it_one_event_each(self):
        column = self.column('IN_PROGRESS')
        with self.published() as publisher:
            set_column_stale_days(column, actor=self.owner, days=3)
        self.assertEqual(len(structure_events(publisher)), 1)
        column.refresh_from_db()
        self.assertEqual(column.stale_after_days, 3)
        with self.published() as publisher:
            set_column_stale_days(column, actor=self.admin, days=5)
        self.assertEqual(len(structure_events(publisher)), 1)
        column.refresh_from_db()
        self.assertEqual(column.stale_after_days, 5)

    def test_the_same_threshold_again_is_nothing(self):
        column = self.column('IN_PROGRESS')
        set_column_stale_days(column, actor=self.owner, days=3)
        column.refresh_from_db()
        stamp = column.updated_at
        with self.published() as publisher:
            set_column_stale_days(column, actor=self.owner, days='3')
        self.assertEqual(structure_events(publisher), [])
        column.refresh_from_db()
        self.assertEqual(column.updated_at, stamp)

    def test_empty_switches_it_off(self):
        column = self.column('IN_PROGRESS')
        set_column_stale_days(column, actor=self.owner, days=3)
        for empty in (None, ''):
            set_column_stale_days(column, actor=self.owner, days=3)
            with self.published() as publisher:
                set_column_stale_days(column, actor=self.owner, days=empty)
            self.assertEqual(len(structure_events(publisher)), 1)
            column.refresh_from_db()
            self.assertIsNone(column.stale_after_days)
        with self.published() as publisher:
            set_column_stale_days(column, actor=self.owner, days=None)
        self.assertEqual(structure_events(publisher), [])

    def test_bounds(self):
        column = self.column('TODO')
        for days in (1, STALE_DAYS_MAX):
            set_column_stale_days(column, actor=self.owner, days=days)
            column.refresh_from_db()
            self.assertEqual(column.stale_after_days, days)
        for days in (0, -1, STALE_DAYS_MAX + 1, 'abc', '2.5'):
            with self.subTest(days=days):
                with self.published() as publisher, self.assertRaises(BoardError):
                    set_column_stale_days(column, actor=self.owner, days=days)
                self.assertEqual(structure_events(publisher), [])
        column.refresh_from_db()
        self.assertEqual(column.stale_after_days, STALE_DAYS_MAX)

    def test_a_member_and_an_archived_board_are_refused(self):
        column = self.column('TODO')
        with self.published() as publisher:
            with self.assertRaises(BoardError):
                set_column_stale_days(column, actor=self.member, days=3)
            with self.assertRaises(BoardError):
                set_column_stale_days(column, actor=self.outsider, days=3)
        self.assertEqual(structure_events(publisher), [])
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            set_column_stale_days(column, actor=self.owner, days=3)
        column.refresh_from_db()
        self.assertIsNone(column.stale_after_days)

    def test_never_the_closing_column(self):
        done = done_column_of(self.board)
        with self.assertRaisesMessage(BoardError, 'завершающей колонке'):
            set_column_stale_days(done, actor=self.owner, days=3)
        done.refresh_from_db()
        self.assertIsNone(done.stale_after_days)

    def test_the_database_holds_the_same_rule(self):
        for column, days in ((self.column('TODO'), 0), (self.column('TODO'), 366), (done_column_of(self.board), 3)):
            with self.subTest(days=days, done=column.is_done), self.assertRaises(IntegrityError):
                with transaction.atomic():
                    BoardColumn.objects.filter(pk=column.pk).update(stale_after_days=days)

    def test_the_sync_revision_moves(self):
        from realtime.sync import build_sync_state

        before = build_sync_state(self.owner)['revisions']['boards']
        set_column_stale_days(self.column('TODO'), actor=self.owner, days=2)
        self.assertNotEqual(build_sync_state(self.owner)['revisions']['boards'], before)


class StaleRouteTests(ColumnTimeMixin, TestCase):
    def url(self, column):
        return reverse('boards:column_stale', args=[self.board.pk, self.main.pk, column.pk])

    def test_the_right_is_asked_before_the_method(self):
        column = self.column('TODO')
        for user in (self.member, self.outsider):
            self.client.force_login(user)
            for method in ('get', 'post'):
                with self.subTest(user=user.username, method=method):
                    response = getattr(self.client, method)(self.url(column), {'days': '3'})
                    self.assertEqual(response.status_code, 403)
        column.refresh_from_db()
        self.assertIsNone(column.stale_after_days)

    def test_a_get_changes_nothing(self):
        column = self.column('TODO')
        self.client.force_login(self.owner)
        response = self.client.get(self.url(column), {'days': '3'})
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        column.refresh_from_db()
        self.assertIsNone(column.stale_after_days)

    def test_post_sets_and_clears_and_keeps_the_filter(self):
        column = self.column('TODO')
        self.client.force_login(self.owner)
        response = self.client.post(self.url(column) + '?stale=1', {'days': '4'})
        self.assertRedirects(response, board_url(self.board) + '?stale=1', fetch_redirect_response=False)
        column.refresh_from_db()
        self.assertEqual(column.stale_after_days, 4)
        self.client.post(self.url(column), {'days': ''})
        column.refresh_from_db()
        self.assertIsNone(column.stale_after_days)

    def test_a_refusal_is_a_message(self):
        column = self.column('TODO')
        self.client.force_login(self.owner)
        for days in ('400', 'много'):
            response = self.client.post(self.url(column), {'days': days}, follow=True)
            self.assertContains(response, 'Застой задаётся')
        response = self.client.post(
            reverse('boards:column_stale', args=[self.board.pk, self.main.pk, done_column_of(self.board).pk]),
            {'days': '3'}, follow=True,
        )
        self.assertContains(response, 'завершающей колонке')

    def test_the_menu_offers_it_to_the_manager_only_and_not_on_the_closing_column(self):
        set_column_stale_days(self.column('IN_PROGRESS'), actor=self.owner, days=3)
        self.client.force_login(self.owner)
        page = self.client.get(board_url(self.board)).content.decode()
        self.assertEqual(page.count('Застой: подсвечивать через N дней'), 3)
        self.assertIn('value="3" placeholder="выкл."', page)
        self.assertNotIn(
            reverse('boards:column_stale', args=[self.board.pk, self.main.pk, done_column_of(self.board).pk]),
            page,
        )
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board)).content.decode()
        self.assertNotIn('Застой: подсвечивать через N дней', page)
        # The header mark is everybody's.
        self.assertIn('⏱ 3 дн.', page)


class StaleTileTests(ColumnTimeMixin, TestCase):
    def setUp(self):
        self.todo = self.column('TODO')
        set_column_stale_days(self.todo, actor=self.owner, days=3)

    def test_exactly_at_the_threshold_and_not_a_day_earlier(self):
        cards = {days: self.card(f'{days} дн.') for days in (2, 3, 4)}
        for days, card in cards.items():
            age(card, days=days)
        items = tile_items(self.state())
        self.assertFalse(items[cards[2].pk]['is_stale'])
        self.assertTrue(items[cards[3].pk]['is_stale'])
        self.assertTrue(items[cards[4].pk]['is_stale'])
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board)).content.decode()
        self.assertEqual(page.count('board-tile--stale'), 2)
        self.assertEqual(page.count('board-tile__age--stale'), 2)

    def test_a_column_without_a_threshold_never_highlights(self):
        card = self.card(stage='IN_PROGRESS')
        age(card, days=100)
        self.assertFalse(self.item(card)['is_stale'])


class StaleFilterTests(ColumnTimeMixin, TestCase):
    """Three open cards in «Сделать» (3 days), one fresh, one 9 days in «В
    работе» (no threshold), one completed long ago."""

    def setUp(self):
        self.todo = self.column('TODO')
        set_column_stale_days(self.todo, actor=self.owner, days=3)
        self.stuck = self.card('Застряла')
        age(self.stuck, days=3)
        self.fresh = self.card('Свежая')
        self.unwatched = self.card('Без порога', stage='IN_PROGRESS')
        age(self.unwatched, days=9)
        self.done = self.card('Сделано')
        age(self.done, days=30)
        complete_task(task_of(self.done), self.member, 'Готово')

    def titles(self, state):
        return sorted(item['card'].title for column in state['columns'] for item in column['cards'])

    def test_parse_and_query(self):
        filters = parse_board_filters({'stale': '1', 'mine': '1'})
        self.assertTrue(filters.stale)
        self.assertTrue(filters.is_active)
        self.assertEqual(filters.query, 'mine=1&stale=1')
        self.assertEqual(parse_board_filters({'stale': 'yes'}).query, '')

    def test_the_page_keeps_the_stuck_and_every_completed(self):
        state = self.state(stale=True)
        self.assertEqual(self.titles(state), ['Застряла', 'Сделано'])
        done = next(column for column in state['columns'] if column['is_done'])
        self.assertEqual(done['count'], 1)

    def test_a_card_whose_column_is_null_is_judged_by_the_first_working_column(self):
        BoardCard.objects.filter(pk=self.fresh.pk).update(column=None)
        age(self.fresh, days=5)
        self.assertIn('Свежая', self.titles(self.state(stale=True)))

    def test_no_threshold_anywhere_keeps_no_open_card(self):
        set_column_stale_days(self.todo, actor=self.owner, days=None)
        self.assertEqual(self.titles(self.state(stale=True)), ['Сделано'])

    def test_column_counts_agree_with_the_page(self):
        filters = BoardFilters(stale=True)
        state = build_board_state(self.board, self.main, self.member, filters=filters)
        self.assertEqual(
            column_counts(self.main, self.member, filters),
            {str(column['pk']): column['count'] for column in state['columns']},
        )
        self.assertEqual(column_counts(self.main, self.member, filters)[str(self.todo.pk)], 1)

    def test_the_fragment_filters_and_carries_it(self):
        self.client.force_login(self.member)
        data = self.client.get(fragment_url(self.board), {'stale': '1'}).json()
        self.assertIn('Застряла', data['columns_html'])
        self.assertNotIn('Свежая', data['columns_html'])
        self.assertIn('stale=1', data['fragment_url'])
        page = self.client.get(board_url(self.board), {'stale': '1'}).content.decode()
        self.assertIn('name="stale" value="1" checked', page)
        self.assertNotIn('Без порога', page)

    def test_the_drag_json_counts_under_the_filter(self):
        other = self.card('Ещё одна')
        age(other, days=4)
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, other.pk]) + '?stale=1',
            {'column_id': self.column('REVIEW').pk},
            HTTP_X_REQUESTED_WITH='fetch',
        )
        self.assertEqual(response.status_code, 200)
        counts = response.json()['counts']
        self.assertEqual(counts[str(self.todo.pk)], 1)
        self.assertEqual(counts[str(self.column('REVIEW').pk)], 0)

    def test_the_query_count_is_the_same_with_and_without_it(self):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as plain:
            self.client.get(board_url(self.board))
        with CaptureQueriesContext(connection) as stale:
            self.client.get(board_url(self.board), {'stale': '1'})
        self.assertEqual(len(stale), len(plain))


class StaleFingerprintTests(ColumnTimeMixin, TestCase):
    def revision(self):
        self.client.force_login(self.member)
        return self.client.get(fragment_url(self.board)).json()['columns_revision']

    def test_the_columns_move_with_the_threshold_and_not_by_themselves(self):
        card = self.card()
        age(card, days=2)
        first = self.revision()
        self.assertEqual(self.revision(), first)
        # A minute later: nothing moved — days are dates, not seconds.
        later = timezone.now() + datetime.timedelta(minutes=1)
        with mock.patch('django.utils.timezone.now', return_value=later):
            self.assertEqual(self.revision(), first)
        set_column_stale_days(self.column('TODO'), actor=self.owner, days=5)
        second = self.revision()
        self.assertNotEqual(second, first)
        set_column_stale_days(self.column('TODO'), actor=self.owner, days=2)
        self.assertNotEqual(self.revision(), second)
