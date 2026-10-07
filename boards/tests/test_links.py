"""«Связи» between cards (`link_cards()`/`unlink_cards()`), the blocking they
carry — the tile, «Заблокированные», «Ждёт» of «Таблица», «Описание» — and
`BOARD_UNBLOCKED`.

Run with the real board access (`BOARD_ACCESS_ROLES` as shipped): a board is
read by its members, so «a board the user does not read» is real here. Two
boards: «Запуск» (`self.zap`) and «Снабжение» (`self.snb`, a Cyrillic code).
`both` is a member of both, `zap_only` of «Запуск» only, `snb_only` of
«Снабжение» only; the administrator owns both.
"""

import itertools
import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification, NotificationDelivery
from notifications.services import EMAIL_ELIGIBLE_EVENTS
from realtime.events import RealtimeEventType
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events

from ..models import BoardCardEvent, BoardCardLink
from ..selectors import NO_FILTERS, build_board_state, column_counts, parse_board_filters
from ..services import (
    ARCHIVED_MESSAGE,
    LINK_NOT_FOUND,
    LINK_WAITS,
    BoardError,
    archive_board,
    cancel_card,
    complete_card,
    create_board,
    link_cards,
    reopen_card,
    unlink_cards,
)
from .helpers import CSRF_INPUT, board_url, done_column_of, fragment_url, main_sub_board, make_user, new_card
from .test_journal import task_of
from .test_lifecycle import main_of
from .test_table import read_xlsx


_SNB = itertools.count(1)


def snb_code():
    """«СН1», «СН2», … — a Cyrillic board code no other test board has."""
    return f'СН{next(_SNB)}'


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


def unblocked():
    return Notification.objects.filter(event_type=Notification.EventType.BOARD_UNBLOCKED)


class LinkFixture:
    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('links_admin', UserProfile.Role.ADMIN)
        cls.both = make_user('links_both', UserProfile.Role.PDO)
        cls.zap_only = make_user('links_zap', UserProfile.Role.OTK)
        cls.snb_only = make_user('links_snb', UserProfile.Role.TO)
        cls.zap = create_board(
            name='Запуск', code=f'Z{next(_SNB)}', owner=cls.admin, actor=cls.admin,
            member_ids=[cls.both.pk, cls.zap_only.pk],
        )
        cls.snb = create_board(
            name='Снабжение', code=snb_code(), owner=cls.admin, actor=cls.admin,
            member_ids=[cls.both.pk, cls.snb_only.pk],
        )

    def zap_card(self, title='Запуск заказа', *, assignees=None, actor=None, **extra):
        return new_card(self.zap, actor or self.both, title, assignees=assignees or [self.zap_only], **extra)

    def snb_card(self, title='Закупить металл', *, assignees=None, actor=None, **extra):
        return new_card(self.snb, actor or self.both, title, assignees=assignees or [self.snb_only], **extra)

    def link(self, card, other, kind=LINK_WAITS, actor=None):
        return link_cards(card, actor=actor or self.both, other_code=other.code, kind=kind)


# --------------------------------------------------------------------------
# Making and removing a link
# --------------------------------------------------------------------------


class LinkServiceTests(LinkFixture, TestCase):
    def test_by_code_on_the_own_board_and_on_a_readable_other_one(self):
        order = self.zap_card()
        second = self.zap_card('Второй заказ')
        metal = self.snb_card()
        own = self.link(order, second, BoardCardLink.Kind.RELATES)
        self.assertEqual((own.from_card_id, own.to_card_id), (order.pk, second.pk))
        # «Ждёт»: the other card blocks this one — any case, spaces around.
        waits = link_cards(order, actor=self.both, other_code=f'  {metal.code.lower()} ', kind=LINK_WAITS)
        self.assertEqual(
            (waits.from_card_id, waits.to_card_id, waits.kind), (metal.pk, order.pk, BoardCardLink.Kind.BLOCKS),
        )
        blocks = self.link(second, metal, BoardCardLink.Kind.BLOCKS)
        self.assertEqual((blocks.from_card_id, blocks.to_card_id), (second.pk, metal.pk))
        duplicate = self.link(second, order, BoardCardLink.Kind.DUPLICATES)
        self.assertEqual((duplicate.from_card_id, duplicate.to_card_id), (second.pk, order.pk))

    def test_an_unreadable_card_and_a_missing_one_are_the_same_refusal(self):
        order = self.zap_card()
        metal = self.snb_card()
        for code in (metal.code, f'{self.zap.code}-999', 'ZZZ-1', 'не код', ''):
            with self.subTest(code=code):
                with self.assertRaisesMessage(BoardError, LINK_NOT_FOUND):
                    link_cards(order, actor=self.zap_only, other_code=code, kind=LINK_WAITS)
        self.assertFalse(BoardCardLink.objects.exists())
        with self.assertRaisesMessage(BoardError, 'Выберите вид связи.'):
            link_cards(order, actor=self.both, other_code=metal.code, kind='PARENT')

    def test_not_with_itself_and_not_twice(self):
        order = self.zap_card()
        metal = self.snb_card()
        with self.assertRaisesMessage(BoardError, 'с самой собой'):
            self.link(order, order, BoardCardLink.Kind.RELATES)
        self.link(order, metal)
        with self.assertRaisesMessage(BoardError, 'Такая связь уже есть.'):
            self.link(order, metal)
        # The same link said from the other card.
        with self.assertRaisesMessage(BoardError, 'Такая связь уже есть.'):
            self.link(metal, order, BoardCardLink.Kind.BLOCKS)
        self.assertEqual(BoardCardLink.objects.count(), 1)

    def test_relates_is_stored_once_the_smaller_id_first(self):
        first = self.zap_card('Первая')
        second = self.snb_card('Вторая')
        link = self.link(second, first, BoardCardLink.Kind.RELATES)
        self.assertEqual((link.from_card_id, link.to_card_id), (first.pk, second.pk))
        with self.assertRaisesMessage(BoardError, 'Такая связь уже есть.'):
            self.link(first, second, BoardCardLink.Kind.RELATES)
        self.assertEqual(BoardCardLink.objects.count(), 1)

    def test_two_cards_never_wait_for_each_other(self):
        order = self.zap_card()
        metal = self.snb_card()
        self.link(order, metal)  # metal blocks order
        for card, other, kind in (
            (order, metal, BoardCardLink.Kind.BLOCKS),
            (metal, order, LINK_WAITS),
        ):
            with self.subTest(kind=kind):
                with self.assertRaisesMessage(BoardError, 'не могут ждать друг друга'):
                    self.link(card, other, kind)
        # Related both ways is not a cycle.
        self.link(order, metal, BoardCardLink.Kind.RELATES)
        self.assertEqual(BoardCardLink.objects.count(), 2)

    def test_subtasks_may_be_linked(self):
        from ..services import create_subtask

        order = self.zap_card()
        part = create_subtask(order, actor=self.both, title='Раскрой', assignees=[self.zap_only])
        metal = self.snb_card()
        link = self.link(part, metal)
        self.assertEqual(link.to_card_id, part.pk)

    def test_the_right_on_either_side(self):
        order = self.zap_card()
        metal = self.snb_card()
        # Works on «Снабжение» and does not read «Запуск»: not even found.
        with self.assertRaisesMessage(BoardError, LINK_NOT_FOUND):
            self.link(metal, order, BoardCardLink.Kind.BLOCKS, actor=self.snb_only)
        link = self.link(order, metal, actor=self.both)
        # Removing: from either side, by whoever works on one and reads the other.
        for outsider in (self.zap_only, self.snb_only):
            with self.subTest(user=outsider.username):
                with self.assertRaisesMessage(BoardError, 'Удалить связь может'):
                    unlink_cards(link, actor=outsider)
        unlink_cards(link, actor=self.admin)
        self.assertFalse(BoardCardLink.objects.exists())
        with self.assertRaisesMessage(BoardError, 'Связь уже удалена.'):
            unlink_cards(link, actor=self.both)

    def test_an_archived_board_links_nothing(self):
        archived = create_board(
            name='Архивная', code=f'A{next(_SNB)}', owner=self.admin, actor=self.admin,
            member_ids=[self.both.pk],
        )
        old = new_card(archived, self.both, 'Старая', assignees=[self.both])
        metal = self.snb_card()
        link = self.link(old, metal)
        complete_card(old, actor=self.both, execution_comment='Готово')
        archive_board(archived, actor=self.admin)
        with self.assertRaisesMessage(BoardError, ARCHIVED_MESSAGE):
            self.link(old, metal, BoardCardLink.Kind.RELATES)
        # From «Снабжение» the link to an archived card may still be made…
        self.link(metal, old, BoardCardLink.Kind.RELATES)
        # …and removed from its live side.
        unlink_cards(link, actor=self.both)

    def test_the_journal_of_both_cards_names_the_kind_and_the_code_never_the_title(self):
        order = self.zap_card('Секретный заказ')
        metal = self.snb_card('Секретный металл')
        link = self.link(order, metal)
        unlink_cards(link, actor=self.both)
        for card, direction, other in ((order, 'in', metal), (metal, 'out', order)):
            with self.subTest(card=card.code):
                entries = list(
                    BoardCardEvent.objects.filter(card=card, kind__in=['LINKED', 'UNLINKED']).order_by('pk')
                )
                self.assertEqual([entry.kind for entry in entries], ['LINKED', 'UNLINKED'])
                self.assertEqual(entries[0].details, {
                    'link': 'BLOCKS', 'direction': direction, 'other_id': other.pk, 'other_code': other.code,
                })
                self.assertNotIn('Секретный', json.dumps([entry.details for entry in entries], ensure_ascii=False))

    def test_one_event_on_each_board_and_the_sync_revision_moves(self):
        order = self.zap_card()
        metal = self.snb_card()
        same = self.zap_card('Соседняя')
        tokens = {user: build_sync_state(user)['revisions'][REVISION_BOARDS] for user in (self.zap_only, self.snb_only)}
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                link = self.link(order, metal)
            self.assertEqual(
                sorted((event.data['board_id'], event.data['card_id'], event.data['change']) for event in board_events(publisher)),
                sorted([(self.zap.pk, order.pk, 'card_updated'), (self.snb.pk, metal.pk, 'card_updated')]),
            )
            for event in publisher.events:
                self.assertNotIn('Закупить', json.dumps(event.as_dict(), ensure_ascii=False))
        for user, token in tokens.items():
            self.assertNotEqual(build_sync_state(user)['revisions'][REVISION_BOARDS], token)
        # Two cards of one board: one event.
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.link(order, same, BoardCardLink.Kind.RELATES)
            self.assertEqual(len(board_events(publisher)), 1)
        # A refusal publishes nothing.
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(BoardError):
                    self.link(order, metal)
            self.assertEqual(publisher.events, [])
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                unlink_cards(link, actor=self.both)
            self.assertEqual(len(board_events(publisher)), 2)


# --------------------------------------------------------------------------
# Blocking: the tile, the filter, the table, «Описание»
# --------------------------------------------------------------------------


class BlockingDisplayTests(LinkFixture, TestCase):
    def setUp(self):
        self.order = self.zap_card('Запуск заказа', assignees=[self.zap_only, self.both])
        self.free = self.zap_card('Свободная')
        self.metal = self.snb_card('Закупить металл')
        self.paint = self.snb_card('Закупить краску')
        self.link(self.order, self.metal)
        self.link(self.order, self.paint)

    def tile_of(self, content, card):
        start = content.index(f'data-card-id="{card.pk}"')
        return content[start:content.index('</li>', start)]

    def test_the_tile_names_the_first_open_blocker_and_counts_the_rest(self):
        self.client.force_login(self.both)
        content = main_of(self.client.get(board_url(self.zap)))
        tile = self.tile_of(content, self.order)
        self.assertIn(f'⛔ ждёт {self.metal.code} +1', tile)
        self.assertNotIn('⛔', self.tile_of(content, self.free))
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        tile = self.tile_of(main_of(self.client.get(board_url(self.zap))), self.order)
        self.assertIn(f'⛔ ждёт {self.paint.code}<', tile)
        cancel_card(self.paint, actor=self.both, reason='Не нужна')
        self.assertNotIn('⛔', self.tile_of(main_of(self.client.get(board_url(self.zap))), self.order))

    def test_a_reader_of_one_board_reads_no_code_of_the_other(self):
        self.client.force_login(self.zap_only)
        content = main_of(self.client.get(board_url(self.zap)))
        self.assertIn('⛔ ждёт карточку другой доски +1', self.tile_of(content, self.order))
        self.assertNotIn(self.metal.code, content)

    def test_the_blocked_filter_keeps_the_waiting_cards_and_leaves_the_closing_column_alone(self):
        done = self.zap_card('Готовая', assignees=[self.both])
        complete_card(done, actor=self.both, execution_comment='Готово')
        filters = parse_board_filters({'blocked': '1'})
        self.assertTrue(filters.is_active)
        self.assertEqual(filters.query, 'blocked=1')
        state = build_board_state(self.zap, main_sub_board(self.zap), self.both, filters=filters)
        shown = {item['card'].pk for column in state['columns'] for item in column['cards']}
        self.assertEqual(shown, {self.order.pk, done.pk})
        counts = column_counts(main_sub_board(self.zap), self.both, filters)
        self.assertEqual(counts[str(done_column_of(self.zap).pk)], 1)
        self.assertEqual(sum(counts.values()), 2)
        # The blocker done: nothing waits any more.
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        complete_card(self.paint, actor=self.snb_only, execution_comment='Есть')
        state = build_board_state(self.zap, main_sub_board(self.zap), self.both, filters=filters)
        shown = {item['card'].pk for column in state['columns'] for item in column['cards']}
        self.assertEqual(shown, {done.pk})
        # The page: the box is there and ticked.
        self.client.force_login(self.both)
        content = main_of(self.client.get(board_url(self.zap), {'blocked': '1'}))
        self.assertIn('name="blocked" value="1" checked', content)

    def test_the_table_and_its_excel_say_what_a_card_waits_for(self):
        self.client.force_login(self.both)
        url = board_url(self.zap)
        page = main_of(self.client.get(url, {'view': 'table'}))
        self.assertIn(f'⛔ {self.metal.code}, {self.paint.code}', page)
        rows, _ = read_xlsx(self.client.get(url, {'view': 'table', 'export': 'xlsx'}).content)
        header = [cell[1] for cell in rows[0]]
        waits = header.index('Ждёт')
        by_code = {row[0][1]: row for row in rows[1:]}
        self.assertEqual(by_code[self.order.code][waits], ('s', f'{self.metal.code}, {self.paint.code}'))
        self.assertEqual(by_code[self.free.code][waits], ('', None))
        # Only the codes of boards the reader reads.
        self.client.force_login(self.zap_only)
        rows, _ = read_xlsx(self.client.get(url, {'view': 'table', 'export': 'xlsx'}).content)
        by_code = {row[0][1]: row for row in rows[1:]}
        self.assertEqual(by_code[self.order.code][waits], ('', None))
        # «Заблокированные» in the table: the open waiting cards only.
        self.client.force_login(self.both)
        page = main_of(self.client.get(url, {'view': 'table', 'blocked': '1'}))
        self.assertIn(self.order.code, page)
        self.assertNotIn(f'>{self.free.code}<', page)

    def test_the_description_groups_the_links_and_hides_an_unreadable_card(self):
        self.link(self.order, self.free, BoardCardLink.Kind.RELATES)
        self.client.force_login(self.both)
        content = main_of(self.client.get(board_url(self.zap), {'card': self.order.pk}))
        links = content.split('data-live-board-links', 1)[1].split('</section>', 1)[0]
        self.assertIn('Ждёт', links)
        self.assertIn('Связана', links)
        self.assertIn(self.metal.code, links)
        self.assertIn('Закупить металл', links)
        self.assertIn('Снабжение', links)
        self.assertIn('ждёт открытых: 2', links)
        self.client.force_login(self.zap_only)
        content = main_of(self.client.get(board_url(self.zap), {'card': self.order.pk}))
        self.assertIn('карточка другой доски', content)
        for secret in (self.metal.code, 'Закупить металл', 'Снабжение', self.paint.code):
            self.assertNotIn(secret, content)
        # Nor in «Лог».
        log = content.split('data-live-board-log', 1)[1]
        self.assertIn('Связь: ждёт карточка другой доски', log)

    def test_the_links_block_is_live_and_the_same_in_the_fragment(self):
        self.client.force_login(self.both)
        page = self.client.get(board_url(self.zap), {'card': self.order.pk}).content.decode()
        fragment = self.client.get(fragment_url(self.zap), {'card': self.order.pk}).json()
        self.assertIn(CSRF_INPUT.sub('', fragment['links_html']), CSRF_INPUT.sub('', page))
        self.assertIn(f'data-links-revision="{fragment["links_revision"]}"', page)
        before = fragment['links_revision']
        panel_before = fragment['panel_revision']
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        fragment = self.client.get(fragment_url(self.zap), {'card': self.order.pk}).json()
        self.assertNotEqual(fragment['links_revision'], before)
        # A blocker closed elsewhere never raises the conflict banner.
        self.assertEqual(fragment['panel_revision'], panel_before)

    def test_one_query_for_the_links_whatever_their_number(self):
        self.client.force_login(self.both)

        def count():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(board_url(self.zap), {'card': self.order.pk})
            return len(queries)

        baseline = count()
        for index in range(4):
            self.link(self.order, self.snb_card(f'Ещё {index}'))
            self.link(self.zap_card(f'Рядом {index}'), self.order, BoardCardLink.Kind.RELATES)
        self.assertEqual(count(), baseline)


# --------------------------------------------------------------------------
# «Можно начинать»
# --------------------------------------------------------------------------


class UnblockedNotificationTests(LinkFixture, TestCase):
    def setUp(self):
        self.order = self.zap_card('Запуск заказа', assignees=[self.zap_only, self.both])
        self.metal = self.snb_card('Закупить металл')
        self.paint = self.snb_card('Закупить краску')
        self.link(self.order, self.metal)
        self.link(self.order, self.paint)

    def test_only_the_last_blocker_tells_and_by_email(self):
        self.assertIn(Notification.EventType.BOARD_UNBLOCKED, EMAIL_ELIGIBLE_EVENTS)
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        self.assertFalse(unblocked().exists())
        complete_card(self.paint, actor=self.snb_only, execution_comment='Есть')
        notes = {note.recipient: note for note in unblocked()}
        self.assertEqual(set(notes), {self.zap_only, self.both})
        self.assertEqual(
            notes[self.both].title, f'Карточку {self.order.code} можно начинать: {self.paint.code} выполнена',
        )
        # Who does not read «Снабжение» reads no code of it.
        self.assertEqual(
            notes[self.zap_only].title,
            f'Карточку {self.order.code} можно начинать: карточка другой доски выполнена',
        )
        self.assertNotIn(self.paint.code, notes[self.zap_only].message)
        for note in notes.values():
            self.assertEqual(note.source_type, Notification.SourceType.TASK)
            self.assertEqual(note.related_task, task_of(self.order))
            self.assertTrue(NotificationDelivery.objects.filter(notification=note).exists())

    def test_a_cancelled_blocker_frees_too_and_says_so(self):
        cancel_card(self.metal, actor=self.admin, reason='Не нужна')
        self.assertFalse(unblocked().exists())
        cancel_card(self.paint, actor=self.admin, reason='Не нужна')
        self.assertEqual(unblocked().count(), 2)
        self.assertTrue(all(note.title.endswith('отменена') for note in unblocked()))

    def test_reopening_blocks_again_silently_and_a_new_closing_tells_again(self):
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        complete_card(self.paint, actor=self.snb_only, execution_comment='Есть')
        self.assertEqual(unblocked().count(), 2)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                reopen_card(self.paint, actor=self.admin)
            # «Снабжение» hears its own card, «Запуск» its waiting tile.
            self.assertIn(
                (self.zap.pk, self.order.pk, 'card_updated'),
                [(e.data['board_id'], e.data['card_id'], e.data['change']) for e in board_events(publisher)],
            )
        self.assertEqual(unblocked().count(), 2)
        state = build_board_state(self.zap, main_sub_board(self.zap), self.both, filters=NO_FILTERS)
        tile = next(item for column in state['columns'] for item in column['cards'] if item['card'].pk == self.order.pk)
        self.assertEqual((tile['blocker_count'], tile['first_blocker_code']), (1, self.paint.code))
        complete_card(self.paint, actor=self.snb_only, execution_comment='Снова есть')
        self.assertEqual(unblocked().count(), 4)

    def test_not_the_one_who_closed_it_and_not_for_a_closed_card(self):
        # `both` works on the waiting card and closes its last blocker.
        own = self.snb_card('Своя', assignees=[self.both])
        waiting = self.zap_card('Ждёт свою', assignees=[self.zap_only, self.both])
        self.link(waiting, own)
        complete_card(own, actor=self.both, execution_comment='Есть')
        self.assertEqual([note.recipient for note in unblocked()], [self.zap_only])
        # A waiting card already closed hears nothing.
        other = self.zap_card('Другая', assignees=[self.zap_only])
        blocker = self.snb_card('Блокер')
        self.link(other, blocker)
        cancel_card(other, actor=self.both, reason='Не нужна')
        complete_card(blocker, actor=self.snb_only, execution_comment='Есть')
        self.assertEqual(unblocked().count(), 1)

    def test_the_closing_announces_the_waiting_board(self):
        complete_card(self.metal, actor=self.snb_only, execution_comment='Есть')
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                complete_card(self.paint, actor=self.snb_only, execution_comment='Есть')
            changes = sorted(
                (e.data['board_id'], e.data['card_id'], e.data['change']) for e in board_events(publisher)
            )
        self.assertEqual(changes, sorted([
            (self.snb.pk, self.paint.pk, 'card_completed'),
            (self.zap.pk, self.order.pk, 'card_updated'),
        ]))


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


class LinkViewTests(LinkFixture, TestCase):
    def setUp(self):
        self.order = self.zap_card()
        self.metal = self.snb_card()

    def link_url(self, card, board):
        return reverse('boards:card_link', args=[board.pk, card.pk])

    def test_link_and_unlink_through_the_drawer(self):
        self.client.force_login(self.both)
        response = self.client.post(self.link_url(self.order, self.zap), {'code': self.metal.code, 'kind': 'WAITS'})
        self.assertRedirects(response, f'{board_url(self.zap)}?card={self.order.pk}', fetch_redirect_response=False)
        link = BoardCardLink.objects.get()
        response = self.client.post(reverse('boards:card_unlink', args=[self.snb.pk, self.metal.pk, link.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(BoardCardLink.objects.exists())

    def test_a_refusal_comes_back_with_what_was_typed(self):
        self.client.force_login(self.zap_only)
        response = self.client.post(self.link_url(self.order, self.zap), {'code': self.metal.code, 'kind': 'RELATES'})
        self.assertEqual(response.status_code, 200)
        content = main_of(response)
        self.assertIn(LINK_NOT_FOUND, content)
        self.assertIn(f'value="{self.metal.code}"', content)
        self.assertIn('<option value="RELATES" selected', content)

    def test_the_right_before_the_method(self):
        self.client.force_login(self.snb_only)
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method.__name__):
                self.assertEqual(method(self.link_url(self.order, self.zap)).status_code, 403)
        link = self.link(self.order, self.metal)
        url = reverse('boards:card_unlink', args=[self.snb.pk, self.metal.pk, link.pk])
        self.assertEqual(self.client.post(url).status_code, 403)
        # Through a card the link does not touch: a 404.
        self.client.force_login(self.both)
        other = self.snb_card('Другая')
        url = reverse('boards:card_unlink', args=[self.snb.pk, other.pk, link.pk])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.assertTrue(BoardCardLink.objects.exists())
