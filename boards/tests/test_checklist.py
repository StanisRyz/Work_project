"""A card's «Чек-лист»: the services, the journal, the routes, the tile, the
table and its Excel, and the live block of its own."""

import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from realtime.events import RealtimeEventType
from realtime.fragments import content_revision
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events
from tasks.services import complete_task

from ..models import MAX_CHECKLIST_ITEMS, BoardCardChecklistItem, BoardCardEvent
from ..selectors import checklist_counts, describe_card_event
from ..services import (
    BoardError,
    add_checklist_item,
    archive_board,
    cancel_card,
    delete_checklist_item,
    move_checklist_item,
    rename_checklist_item,
    toggle_checklist_item,
)
from .helpers import (
    LIVE_BLOCKS,
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    fragment_url,
)
from .test_journal import task_of
from .test_table import read_xlsx


FETCH = {'HTTP_X_REQUESTED_WITH': 'fetch'}

# The page's live blocks, the card's «Чек-лист» among them.
CHECKLIST_BLOCKS = {**LIVE_BLOCKS, 'checklist': ('checklist_html',)}


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


def entries(card):
    return list(BoardCardEvent.objects.filter(card=card, kind=BoardCardEvent.Kind.CHECKLIST).order_by('pk'))


def texts(card):
    return list(BoardCardChecklistItem.objects.filter(card=card).order_by('position').values_list('text', flat=True))


class ChecklistMixin(BoardFixtureMixin):
    def setUp(self):
        self.card_obj = self.card('Запуск заказа', assignees=[self.member])

    def add(self, *items, actor=None):
        return [add_checklist_item(self.card_obj, actor=actor or self.member, text=text) for text in items]


# --------------------------------------------------------------------------
# The services
# --------------------------------------------------------------------------


class ChecklistServiceTests(ChecklistMixin, TestCase):
    def test_add_rename_toggle_move_delete(self):
        first, second, third = self.add('  Спецификация  ', 'Металл', 'Запуск в цех')
        self.assertEqual(texts(self.card_obj), ['Спецификация', 'Металл', 'Запуск в цех'])
        self.assertEqual([item.position for item in (first, second, third)], [1, 2, 3])

        rename_checklist_item(second, actor=self.member, text='Закупить металл')
        toggle_checklist_item(first, actor=self.colleague)
        first.refresh_from_db()
        self.assertTrue(first.is_done)
        self.assertEqual(first.done_by, self.colleague)
        self.assertIsNotNone(first.done_at)
        self.assertEqual(checklist_counts(self.card_obj), (1, 3))

        move_checklist_item(third, actor=self.member, direction='up')
        self.assertEqual(texts(self.card_obj), ['Спецификация', 'Запуск в цех', 'Закупить металл'])

        toggle_checklist_item(first, actor=self.member, done=False)
        first.refresh_from_db()
        self.assertEqual((first.is_done, first.done_by, first.done_at), (False, None, None))

        delete_checklist_item(third, actor=self.admin)
        self.assertFalse(BoardCardChecklistItem.objects.filter(pk=third.pk).exists())
        self.assertEqual(
            list(BoardCardChecklistItem.objects.filter(card=self.card_obj).order_by('position')
                 .values_list('text', 'position')),
            [('Спецификация', 1), ('Закупить металл', 2)],
        )

    def test_who_may_change_it(self):
        (item,) = self.add('Спецификация')
        self.add('От администратора', actor=self.admin)
        # The outsider reads the board (full access in these tests) but is no member.
        for call in (
            lambda: add_checklist_item(self.card_obj, actor=self.outsider, text='Чужой'),
            lambda: rename_checklist_item(item, actor=self.outsider, text='Чужой'),
            lambda: toggle_checklist_item(item, actor=self.outsider),
            lambda: move_checklist_item(item, actor=self.outsider, direction='down'),
            lambda: delete_checklist_item(item, actor=self.outsider),
        ):
            with self.subTest(call=call), self.assertRaises(BoardError):
                call()
        self.assertEqual(texts(self.card_obj), ['Спецификация', 'От администратора'])

    def test_a_closed_card_and_an_archived_board_are_refused(self):
        (item,) = self.add('Спецификация')
        cancelled = self.card('Отменённая')
        (other,) = [add_checklist_item(cancelled, actor=self.member, text='Шаг')]
        cancel_card(cancelled, actor=self.member, reason='Не нужна')
        complete_task(task_of(self.card_obj), self.member, 'Готово')
        for card, entry in ((self.card_obj, item), (cancelled, other)):
            for call in (
                lambda: add_checklist_item(card, actor=self.member, text='Ещё'),
                lambda: rename_checklist_item(entry, actor=self.member, text='Иначе'),
                lambda: toggle_checklist_item(entry, actor=self.member),
                lambda: move_checklist_item(entry, actor=self.member, direction='down'),
                lambda: delete_checklist_item(entry, actor=self.member),
            ):
                with self.subTest(card=card.title), self.assertRaisesMessage(BoardError, 'закрыта'):
                    call()
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            toggle_checklist_item(item, actor=self.admin)
        item.refresh_from_db()
        self.assertFalse(item.is_done)

    def test_the_limit_of_items(self):
        BoardCardChecklistItem.objects.bulk_create([
            BoardCardChecklistItem(card=self.card_obj, text=f'Шаг {index}', position=index, created_by=self.member)
            for index in range(1, MAX_CHECKLIST_ITEMS)
        ])
        self.add('Последний')
        with self.assertRaisesMessage(BoardError, str(MAX_CHECKLIST_ITEMS)):
            self.add('Лишний')
        self.assertEqual(BoardCardChecklistItem.objects.filter(card=self.card_obj).count(), MAX_CHECKLIST_ITEMS)

    def test_empty_and_long_text_are_refused(self):
        (item,) = self.add('я' * 200)
        for text in ('', '   ', 'я' * 201):
            with self.subTest(length=len(text)):
                with self.assertRaises(BoardError):
                    self.add(text)
                with self.assertRaises(BoardError):
                    rename_checklist_item(item, actor=self.member, text=text)
        self.assertEqual(texts(self.card_obj), ['я' * 200])

    def test_an_item_of_another_card_is_not_found(self):
        other = self.card('Другая')
        (foreign,) = [add_checklist_item(other, actor=self.member, text='Чужой шаг')]
        from ..services import _checklist_item

        with self.assertRaises(BoardError):
            _checklist_item(self.card_obj, foreign, 'test', actor=self.member)

    def test_the_same_again_writes_nothing(self):
        first, second = self.add('Первый', 'Второй')
        toggle_checklist_item(first, actor=self.member, done=True)
        card_stamp = type(self.card_obj).objects.get(pk=self.card_obj.pk).updated_at
        journal = len(entries(self.card_obj))
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                rename_checklist_item(first, actor=self.member, text='  Первый ')
                toggle_checklist_item(first, actor=self.member, done=True)
                move_checklist_item(first, actor=self.member, direction='up')
                move_checklist_item(second, actor=self.member, direction='down')
            self.assertEqual(board_events(publisher), [])
        self.assertEqual(len(entries(self.card_obj)), journal)
        self.assertEqual(type(self.card_obj).objects.get(pk=self.card_obj.pk).updated_at, card_stamp)

    def test_one_event_per_success_and_none_on_refusal(self):
        (item,) = self.add('Первый')
        token = build_sync_state(self.member)['revisions'][REVISION_BOARDS]
        for call in (
            lambda: add_checklist_item(self.card_obj, actor=self.member, text='Второй'),
            lambda: rename_checklist_item(item, actor=self.member, text='Первый шаг'),
            lambda: toggle_checklist_item(item, actor=self.member),
            lambda: move_checklist_item(item, actor=self.member, direction='down'),
            lambda: delete_checklist_item(item, actor=self.member),
        ):
            with self.subTest(call=call):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        call()
                    events = board_events(publisher)
                    self.assertEqual(
                        [(event.data['change'], event.data['card_id']) for event in events],
                        [('checklist_changed', self.card_obj.pk)],
                    )
                    for event in publisher.events:
                        self.assertNotIn('шаг', json.dumps(event.as_dict(), ensure_ascii=False).lower())
                # The safety net of a lost event moves with every write.
                new_token = build_sync_state(self.member)['revisions'][REVISION_BOARDS]
                self.assertNotEqual(new_token, token)
                token = new_token
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(BoardError):
                    self.add('')
            self.assertEqual(publisher.events, [])

    def test_the_journal_names_the_action_and_the_counts_never_the_text(self):
        first, second = self.add('Секретный шаг', 'Второй')
        toggle_checklist_item(first, actor=self.member)
        rename_checklist_item(second, actor=self.member, text='Тайный')
        move_checklist_item(second, actor=self.member, direction='up')
        toggle_checklist_item(first, actor=self.member)
        delete_checklist_item(second, actor=self.member)
        rows = entries(self.card_obj)
        self.assertEqual(
            [row.details for row in rows],
            [
                {'action': 'added', 'item_id': first.pk, 'done': 0, 'total': 1},
                {'action': 'added', 'item_id': second.pk, 'done': 0, 'total': 2},
                {'action': 'done', 'item_id': first.pk, 'done': 1, 'total': 2},
                {'action': 'renamed', 'item_id': second.pk, 'done': 1, 'total': 2},
                {'action': 'undone', 'item_id': first.pk, 'done': 0, 'total': 2},
                {'action': 'deleted', 'item_id': second.pk, 'done': 0, 'total': 1},
            ],
        )
        for row in rows:
            self.assertNotIn('Секрет', json.dumps(row.details, ensure_ascii=False))
            self.assertNotIn('Тайн', json.dumps(row.details, ensure_ascii=False))
        self.assertEqual(
            [describe_card_event(row) for row in rows],
            [
                'Чек-лист: добавлен пункт (0/1)',
                'Чек-лист: добавлен пункт (0/2)',
                'Чек-лист: отмечен пункт (1/2)',
                'Чек-лист: пункт переименован (1/2)',
                'Чек-лист: снята отметка с пункта (0/2)',
                'Чек-лист: удалён пункт (0/1)',
            ],
        )

    def test_a_checklist_entry_does_not_restart_the_clock_in_the_column(self):
        from ..selectors import in_column_since
        from tasks.models import Task

        since = Task.objects.annotate(since=in_column_since()).get(pk=task_of(self.card_obj).pk).since
        self.add('Шаг')
        self.assertEqual(
            Task.objects.annotate(since=in_column_since()).get(pk=task_of(self.card_obj).pk).since, since,
        )

    def test_completing_needs_no_ticked_item(self):
        self.add('Не сделано')
        complete_task(task_of(self.card_obj), self.member, 'Готово без шагов')
        self.assertEqual(task_of(self.card_obj).status.code, 'COMPLETED')


# --------------------------------------------------------------------------
# The routes
# --------------------------------------------------------------------------


class ChecklistRouteTests(ChecklistMixin, TestCase):
    def url(self, name, item=None):
        args = [self.board.pk, self.card_obj.pk] + ([item.pk] if item is not None else [])
        return reverse(f'boards:{name}', args=args)

    def card_page(self):
        return f'{board_url(self.board)}?card={self.card_obj.pk}&tab=description'

    def test_the_right_is_asked_before_the_method(self):
        (item,) = self.add('Шаг')
        self.client.force_login(self.outsider)
        for name, with_item in (
            ('checklist_add', False), ('checklist_rename', True), ('checklist_toggle', True),
            ('checklist_move', True), ('checklist_delete', True),
        ):
            url = self.url(name, item if with_item else None)
            for method in ('get', 'post'):
                with self.subTest(name=name, method=method):
                    response = getattr(self.client, method)(url, {'text': 'x', 'done': '1', 'direction': 'up'})
                    self.assertEqual(response.status_code, 403)
        response = self.client.post(self.url('checklist_toggle', item), {'done': '1'}, **FETCH)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['ok'], False)
        item.refresh_from_db()
        self.assertFalse(item.is_done)
        self.assertEqual(texts(self.card_obj), ['Шаг'])

    def test_a_get_changes_nothing(self):
        (item,) = self.add('Шаг')
        self.client.force_login(self.member)
        for name in ('checklist_rename', 'checklist_toggle', 'checklist_move', 'checklist_delete'):
            with self.subTest(name=name):
                response = self.client.get(self.url(name, item))
                self.assertRedirects(response, self.card_page(), fetch_redirect_response=False)
        response = self.client.get(self.url('checklist_add'))
        self.assertRedirects(response, self.card_page(), fetch_redirect_response=False)
        item.refresh_from_db()
        self.assertEqual((item.text, item.is_done), ('Шаг', False))
        self.assertEqual(len(entries(self.card_obj)), 1)

    def test_the_forms_without_javascript(self):
        self.client.force_login(self.member)
        response = self.client.post(self.url('checklist_add') + '?mine=1', {'text': 'Спецификация'})
        self.assertRedirects(response, self.card_page() + '&mine=1', fetch_redirect_response=False)
        item = BoardCardChecklistItem.objects.get(card=self.card_obj)
        self.client.post(self.url('checklist_toggle', item), {'done': '1'})
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        # The same state asked again — a double click — changes nothing.
        self.client.post(self.url('checklist_toggle', item), {'done': '1'})
        item.refresh_from_db()
        self.assertTrue(item.is_done)
        self.client.post(self.url('checklist_rename', item), {'text': 'Утвердить спецификацию'})
        self.client.post(self.url('checklist_add'), {'text': 'Металл'})
        self.client.post(self.url('checklist_move', item), {'direction': 'down'})
        self.assertEqual(texts(self.card_obj), ['Металл', 'Утвердить спецификацию'])
        self.client.post(self.url('checklist_delete', item))
        self.assertEqual(texts(self.card_obj), ['Металл'])

    def test_a_refusal_keeps_what_was_typed(self):
        self.client.force_login(self.member)
        long_text = 'Очень длинный пункт ' * 15
        response = self.client.post(self.url('checklist_add'), {'text': long_text})
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('не длиннее 200', content)
        self.assertIn(f'value="{long_text}"', content)
        (item,) = self.add('Шаг')
        response = self.client.post(self.url('checklist_rename', item), {'text': '   '})
        content = response.content.decode()
        self.assertIn('data-checklist-edit', content)
        self.assertIn('Напишите пункт', content)
        self.assertEqual(texts(self.card_obj), ['Шаг'])

    def test_a_tick_from_the_script_is_answered_in_json(self):
        first, _second = self.add('Первый', 'Второй')
        self.client.force_login(self.member)
        response = self.client.post(self.url('checklist_toggle', first), {'done': '1'}, **FETCH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(), {'ok': True, 'item_id': first.pk, 'is_done': True, 'done': 1, 'total': 2},
        )
        complete_task(task_of(self.card_obj), self.member, 'Готово')
        response = self.client.post(self.url('checklist_toggle', first), {'done': '0'}, **FETCH)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['ok'], False)
        self.assertIn('закрыта', response.json()['error'])
        first.refresh_from_db()
        self.assertTrue(first.is_done)


# --------------------------------------------------------------------------
# The panel, the tile, the table, the live block
# --------------------------------------------------------------------------


class ChecklistDisplayTests(ChecklistMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.items = self.add('Спецификация', 'Металл', 'Цех', 'ОТК', 'Отгрузка')
        for item in self.items[:2]:
            toggle_checklist_item(item, actor=self.member)

    def fragment(self, user=None, **query):
        self.client.force_login(user or self.member)
        return self.client.get(fragment_url(self.board), {'card': self.card_obj.pk, **query}).json()

    def test_the_tile_says_two_of_five(self):
        self.client.force_login(self.member)
        columns = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn('☑ 2/5', columns)
        self.assertNotIn('board-tile__checklist--complete', columns)
        for item in self.items[2:]:
            toggle_checklist_item(item, actor=self.member)
        columns = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn('☑ 5/5', columns)
        self.assertIn('board-tile__checklist--complete', columns)
        # A card without items shows nothing.
        self.card('Без чек-листа')
        self.assertEqual(self.client.get(fragment_url(self.board)).json()['columns_html'].count('☑'), 1)

    def test_the_block_on_the_description(self):
        html = self.fragment()['checklist_html']
        self.assertIn('Чек-лист', html)
        self.assertIn('data-checklist-count>2/5<', html)
        self.assertEqual(html.count('class="board-checklist__item is-done"'), 2)
        self.assertIn('title="Отметил member_one,', html)
        self.assertEqual(html.count('data-checklist-toggle'), 5)
        drawer = self.fragment()['drawer_html']
        self.assertIn(reverse('boards:checklist_add', args=[self.board.pk, self.card_obj.pk]), drawer)
        self.assertIn('placeholder="Добавить пункт"', drawer)
        # A reader who may not change it sees the list, and nothing that posts.
        html = self.fragment(self.outsider)['checklist_html']
        self.assertIn('2/5', html)
        self.assertNotIn('<form', html)
        self.assertNotIn('/checklist/add/', self.fragment(self.outsider)['drawer_html'])

    def test_the_rename_form_by_address(self):
        target = self.items[3]
        payload = self.fragment(tab='description', edit_item=target.pk)
        self.assertIn('data-checklist-edit', payload['checklist_html'])
        self.assertIn(f'value="{target.text}"', payload['checklist_html'])
        self.assertIn(f'edit_item={target.pk}', payload['fragment_url'])
        # An item of another card, or text, opens no form.
        for value in ('abc', '999999'):
            self.assertNotIn('data-checklist-edit', self.fragment(edit_item=value)['checklist_html'])

    def test_the_page_prints_the_fragments_blocks(self):
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.card_obj.pk}).content.decode()
        assert_page_matches_fragment(self, page, self.fragment(), CHECKLIST_BLOCKS)

    def test_a_tick_moves_the_checklist_and_the_columns_never_the_panel(self):
        before = self.fragment()
        toggle_checklist_item(self.items[2], actor=self.colleague)
        after = self.fragment()
        self.assertEqual(after['panel_revision'], before['panel_revision'])
        self.assertNotEqual(after['checklist_revision'], before['checklist_revision'])
        self.assertNotEqual(after['columns_revision'], before['columns_revision'])
        self.assertIn('3/5', after['checklist_html'])
        self.assertEqual(after['checklist_revision'], content_revision(after['checklist_html']))
        # Nor with the card's edit form open.
        edit_before = self.fragment(edit='1')
        toggle_checklist_item(self.items[3], actor=self.colleague)
        edit_after = self.fragment(edit='1')
        self.assertEqual(edit_after['panel_revision'], edit_before['panel_revision'])
        self.assertIn('4/5', edit_after['checklist_html'])

    def test_the_log_words_it(self):
        log = self.fragment()['log_html']
        self.assertIn('Чек-лист: отмечен пункт (2/5)', log)
        self.assertNotIn('Спецификация', log)

    def test_the_table_and_its_excel(self):
        self.client.force_login(self.member)
        self.card('Без чек-листа')
        page = self.client.get(board_url(self.board), {'view': 'table'}).content.decode()
        self.assertIn('<th class="board-table__checklist">Чек-лист</th>', page)
        self.assertIn('>2/5</span>', page)
        rows, _ = read_xlsx(
            self.client.get(board_url(self.board), {'view': 'table', 'export': 'xlsx'}).content,
        )
        header = [value for _, value in rows[0]]
        column = header.index('Чек-лист')
        self.assertEqual(header[column - 1], 'В колонке, дн.')
        cells = {row[0][1]: row[column] for row in rows[1:]}
        self.assertEqual(cells[self.card_obj.code], ('s', '2/5'))
        self.assertIn(('', None), cells.values())

    def _queries(self):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(board_url(self.board), {'card': self.card_obj.pk})
        return len(queries)

    def test_the_count_does_not_grow_with_items(self):
        baseline = self._queries()
        for index in range(10):
            item = add_checklist_item(self.card_obj, actor=self.member, text=f'Шаг {index}')
            if index % 2:
                toggle_checklist_item(item, actor=self.colleague)
        other = self.card('Вторая')
        for index in range(4):
            add_checklist_item(other, actor=self.member, text=f'Шаг {index}')
        # One more tile, the same number of queries: the counts are annotated.
        self.assertEqual(self._queries(), baseline)
