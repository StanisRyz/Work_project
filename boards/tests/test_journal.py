"""Stage 13: the card's journal, `reopen_card()`, and the drawer's tabs."""

import re
import tempfile

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from realtime.events import BOARD_CHANGE_CARD_REOPENED, RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.services import complete_task

from ..models import BoardCardEvent
from ..selectors import describe_card_event
from ..services import (
    BoardError,
    archive_board,
    cancel_card,
    complete_card,
    create_card,
    move_card,
    post_card_comment,
    rename_column,
    reopen_card,
    update_card,
)
from .helpers import (
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    chat_upload,
    done_column_of,
    due,
    fragment_url,
    legacy_attachment,
    page_attribute,
)


# A sub-board page with a card open, for a member of the board who is an
# исполнитель of it (`test_query_count_does_not_grow_with_entries_messages_or_files`).
# 34 at stage 14; one more since the board's card fields are read (none on
# this board, so neither their options nor any value is) — a board with fields
# is measured in `test_fields.FieldQueryCountTests`. Three more since the
# card's «Чек-лист» (its items, one query), its followers (the board's readers
# marked by whether they follow — one query, also the «@» list of «Чат») and
# the mentions of the messages shown (one prefetch, this card has messages).
# Stage 19: one more for the files of «Чат» (every file of the card, one
# query), two fewer — the board's member count is taken from the heading's
# own member list, and the tabs are read once for the page and its
# `build_board_state()` — so 37. Stage 20: two more for the card's
# «Подзадачи» — its subtasks with their tasks and statuses, and their
# исполнители — whatever their number (`test_subtasks`), so 39.
PAGE_QUERIES = 39

MEDIA = override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='board-journal-'))


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def kinds(card):
    return list(card.events.order_by('pk').values_list('kind', flat=True))


class JournalWritesTests(BoardFixtureMixin, TestCase):
    """Every change of a card is exactly one entry; no change is none."""

    def setUp(self):
        self.card_obj = self.card('Сверить остатки', assignees=[self.member])

    def edit(self, **changes):
        task = task_of(self.card_obj)
        values = {
            'title': self.card_obj.title,
            'description': self.card_obj.description,
            'due_date': task.due_date,
            'assignee_ids': list(task.assignees.values_list('user_id', flat=True)),
        }
        values.update(changes)
        self.card_obj = update_card(self.card_obj, actor=self.member, **values)
        return self.card_obj

    def test_create_records_the_column_by_name(self):
        event = self.card_obj.events.get()
        self.assertEqual(event.kind, BoardCardEvent.Kind.CREATED)
        self.assertEqual(event.actor, self.member)
        todo = self.column('TODO')
        self.assertEqual(event.details, {'column_id': todo.pk, 'column': todo.name})

    def test_edit_records_which_fields_and_only_when_something_was_stored(self):
        self.edit()
        self.assertEqual(kinds(self.card_obj), ['CREATED'])
        self.edit(title='Сверить остатки склада', due_date=due(9))
        self.edit(assignee_ids=[self.member.pk, self.colleague.pk])
        self.edit(description='С бухгалтерией')
        edits = list(self.card_obj.events.filter(kind='EDITED').order_by('pk'))
        self.assertEqual(
            [event.details['fields'] for event in edits],
            [['title', 'due_date'], ['assignees'], ['description']],
        )
        self.assertEqual(describe_card_event(edits[0]), 'Изменено: заголовок, срок')

    def test_move_records_both_columns_as_they_were_named(self):
        todo, review = self.column('TODO'), self.column('REVIEW')
        move_card(self.card_obj, actor=self.colleague, column=review)
        event = self.card_obj.events.get(kind='MOVED')
        self.assertEqual(event.actor, self.colleague)
        self.assertEqual(event.details, {
            'from_column_id': todo.pk, 'from_column': todo.name,
            'to_column_id': review.pk, 'to_column': review.name,
        })
        rename_column(review, actor=self.owner, name='Контроль')
        event.refresh_from_db()
        self.assertEqual(describe_card_event(event), f'Перенос: «{todo.name}» → «На проверке»')

    def test_no_entry_for_a_move_in_place_a_reorder_or_a_refusal(self):
        other = self.card('Другая', assignees=[self.member])
        todo = self.column('TODO')
        move_card(self.card_obj, actor=self.member, column=todo)  # where it stands
        move_card(other, actor=self.member, column=todo, before_card_id=self.card_obj.pk)  # reorder
        with self.assertRaises(BoardError):
            move_card(self.card_obj, actor=self.member, column=done_column_of(self.board))
        with self.assertRaises(BoardError):
            move_card(self.card_obj, actor=self.outsider, column=self.column('REVIEW'))
        self.assertFalse(BoardCardEvent.objects.filter(kind='MOVED').exists())

    def test_complete_reopen_cancel(self):
        with self.assertRaises(BoardError):
            complete_card(self.card_obj, actor=self.member, execution_comment='  ')
        complete_card(self.card_obj, actor=self.member, execution_comment='Готово')
        with self.assertRaises(BoardError):
            reopen_card(self.card_obj, actor=self.member)
        reopen_card(self.card_obj, actor=self.admin)
        cancel_card(self.card_obj, actor=self.member, reason='Ошибка')
        self.assertEqual(kinds(self.card_obj), ['CREATED', 'COMPLETED', 'REOPENED', 'CANCELLED'])
        reopened = self.card_obj.events.get(kind='REOPENED')
        self.assertEqual(reopened.actor, self.admin)
        self.assertEqual(reopened.details['column'], self.column('TODO').name)
        # The reason is the card's record, never the journal's.
        self.assertEqual(self.card_obj.events.get(kind='CANCELLED').details, {})

    def test_a_rollback_takes_the_entry_with_it(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            move_card(self.card_obj, actor=self.member, column=self.column('REVIEW'))
            complete_card(self.card_obj, actor=self.member, execution_comment='Готово')
            raise RuntimeError
        with self.assertRaises(RuntimeError), transaction.atomic():
            create_card(
                self.main, actor=self.member, title='Не будет', due_date=due(), assignee_ids=[self.member.pk],
            )
            raise RuntimeError
        self.assertEqual(kinds(self.card_obj), ['CREATED'])
        self.assertEqual(BoardCardEvent.objects.count(), 1)

    def test_tasks_routes_leave_no_gap_because_they_refuse(self):
        self.client.force_login(self.member)
        task = task_of(self.card_obj)
        self.client.post(reverse('tasks:complete', args=[task.pk]), {'execution_comment': 'Мимо доски'})
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertEqual(kinds(self.card_obj), ['CREATED'])


class OtherSourcesTests(BoardFixtureMixin, TestCase):
    """`tasks:complete` and `tasks:reopen` still work for every other task."""

    def test_an_ordinary_task_is_completed_and_reopened_as_before(self):
        from bugs.models import BugReport
        from tasks.services import create_bug_report_task

        report = BugReport.objects.create(reporter=self.colleague, message='Не открывается', page_url='/')
        task = create_bug_report_task(report, [self.member.pk], created_by=self.colleague, due_date=due())
        self.client.force_login(self.member)
        self.client.post(reverse('tasks:complete', args=[task.pk]), {'execution_comment': 'Исправлено'})
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')
        self.client.force_login(self.admin)
        response = self.client.post(reverse('tasks:reopen', args=[task.pk]))
        self.assertRedirects(response, reverse('tasks:detail', args=[task.pk]), fetch_redirect_response=False)
        task.refresh_from_db()
        self.assertEqual(task.status.code, 'IN_PROGRESS')


class ReopenCardTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Сверить остатки', assignees=[self.member], stage='REVIEW')
        self.task = task_of(self.card_obj)
        complete_task(self.task, self.member, 'Готово')
        self.url = reverse('boards:card_reopen', args=[self.board.pk, self.card_obj.pk])
        self.card_url = f'{board_url(self.board)}?card={self.card_obj.pk}'

    def test_one_event_and_the_card_back_in_its_column(self):
        with capture_realtime_events() as publisher, self.captureOnCommitCallbacks(execute=True):
            reopen_card(self.card_obj, actor=self.admin)
        events = publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data, {
            'board_id': self.board.pk, 'card_id': self.card_obj.pk, 'change': BOARD_CHANGE_CARD_REOPENED,
        })
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')
        self.assertEqual(self.task.execution_comment, 'Готово')

    def test_refusals_write_and_publish_nothing(self):
        for actor in (self.member, self.owner, self.outsider):
            with self.subTest(actor=actor.username):
                with capture_realtime_events() as publisher, self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaises(BoardError):
                        reopen_card(self.card_obj, actor=actor)
                self.assertEqual(publisher.events_of_type(RealtimeEventType.BOARD_UPDATED), [])
        reopen_card(self.card_obj, actor=self.admin)
        with self.assertRaises(BoardError):
            reopen_card(self.card_obj, actor=self.admin)   # already open
        self.assertEqual(self.card_obj.events.filter(kind='REOPENED').count(), 1)

    def test_an_archived_board_stays_as_it_was_shelved(self):
        other = self.card('Открытая', assignees=[self.member])
        cancel_card(other, actor=self.member, reason='Не нужна')
        archive_board(self.board, actor=self.owner)
        with self.assertRaises(BoardError):
            reopen_card(self.card_obj, actor=self.admin)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'COMPLETED')
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(self.card_url), self.url)

    def test_route_asks_before_the_method_and_get_changes_nothing(self):
        for user in (self.member, self.outsider, self.owner):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url).status_code, 403)
                self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(self.admin)
        self.assertRedirects(self.client.get(self.url), self.card_url, fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'COMPLETED')
        response = self.client.post(f'{self.url}?mine=1')
        self.assertRedirects(response, f'{self.card_url}&mine=1', fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status.code, 'IN_PROGRESS')
        # Open now: the right is gone, so the route is a 403 again.
        self.assertEqual(self.client.post(self.url).status_code, 403)


@MEDIA
class DrawerTabsTests(BoardFixtureMixin, TestCase):
    TABS = ('description', 'chat', 'log')

    def setUp(self):
        self.card_obj = self.card('Сверить остатки', assignees=[self.member])
        self.task = task_of(self.card_obj)
        self.page = board_url(self.board)
        self.fragment = fragment_url(self.board)
        self.client.force_login(self.member)

    def get(self, **query):
        return self.client.get(self.page, {'card': self.card_obj.pk, **query})

    def test_the_tab_comes_from_the_query_and_unknown_is_description(self):
        cases = [(tab, tab) for tab in self.TABS] + [('', 'description'), ('nope', 'description')]
        for asked, shown in cases:
            with self.subTest(tab=asked):
                content = self.get(tab=asked).content.decode()
                self.assertIn(f'data-board-tab="{shown}"', content)
                active = re.findall(
                    r'class="board-drawer__tab is-active"[^>]*data-board-tab-link="(\w+)"', content,
                )
                self.assertEqual(active, [shown])
                self.assertEqual(
                    page_attribute(content, 'data-board-page-url'),
                    f'{self.page}?card={self.card_obj.pk}&tab={shown}',
                )
                self.assertEqual(
                    page_attribute(content, 'data-board-fragment-url'),
                    f'{self.fragment}?card={self.card_obj.pk}&tab={shown}',
                )

    def test_the_old_files_tab_is_the_chat_showing_its_files(self):
        content = self.get(tab='files').content.decode()
        self.assertIn('data-board-tab="chat"', content)
        self.assertIn('data-board-chat-mode="files"', content)
        self.assertNotIn('data-board-tab-link="files"', content)
        self.assertNotIn('data-board-tab-body="files"', content)
        self.assertEqual(
            page_attribute(content, 'data-board-page-url'),
            f'{self.page}?card={self.card_obj.pk}&tab=chat&chat=files',
        )

    def test_every_tab_body_is_drawn_whatever_the_tab(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='Где файл?')
        legacy_attachment(self.task, self.member, 'протокол.pdf')
        content = self.get(tab='log').content.decode()
        for tab in self.TABS:
            self.assertIn(f'data-board-tab-body="{tab}"', content)
        self.assertIn('Где файл?', content)
        self.assertIn('Добавлен файл «протокол.pdf»', content)
        self.assertIn('data-board-tab-count="chat">1<', content)
        self.assertIn('data-board-chat-files-count>1<', content)
        # Without JavaScript a tab is a link to the same page with `tab`.
        self.assertIn(f'href="{self.page}?card={self.card_obj.pk}&amp;tab=chat"', content)

    def test_page_equals_fragment_for_every_tab(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='Сообщение')
        for tab in self.TABS:
            with self.subTest(tab=tab):
                page = self.get(tab=tab).content.decode()
                payload = self.client.get(self.fragment, {'card': self.card_obj.pk, 'tab': tab}).json()
                assert_page_matches_fragment(self, page, payload)
                self.assertEqual(payload['tab'], tab)
                self.assertEqual(payload['card_id'], self.card_obj.pk)
                self.assertEqual(payload['page_url'], f'{self.page}?card={self.card_obj.pk}&tab={tab}')
                self.assertIn(f'data-board-tab="{tab}"', page)

    def test_a_message_and_a_journal_entry_leave_the_panel_fingerprint_alone(self):
        before = self.client.get(self.fragment, {'card': self.card_obj.pk}).json()
        post_card_comment(self.card_obj, actor=self.colleague, text='Новое')
        BoardCardEvent.objects.create(
            card=self.card_obj, actor=self.colleague, kind='EDITED', details={'fields': []},
        )
        after = self.client.get(self.fragment, {'card': self.card_obj.pk}).json()
        self.assertEqual(before['panel_revision'], after['panel_revision'])
        self.assertNotEqual(before['comments_revision'], after['comments_revision'])
        self.assertNotEqual(before['log_revision'], after['log_revision'])
        self.assertEqual(after['chat_count'], 1)
        # No message in the log, and no journal entry in the chat.
        self.assertNotIn('Новое', after['log_html'])
        self.assertNotIn('Карточка изменена', after['comments_html'])

    def test_the_log_is_newest_first_with_the_files_beside_it(self):
        move_card(self.card_obj, actor=self.colleague, column=self.column('IN_PROGRESS'))
        legacy_attachment(self.task, self.member, 'схема.pdf')
        complete_card(self.card_obj, actor=self.member, execution_comment='Готово')
        log = self.client.get(self.fragment, {'card': self.card_obj.pk}).json()['log_html']
        order = [
            log.index(text) for text in (
                'Задача выполнена', 'Добавлен файл «схема.pdf»', 'Перенос: «Сделать» → «В работе»',
                f'Карточка {self.card_obj.code} создана в колонке «Сделать»',
            )
        ]
        self.assertEqual(order, sorted(order))

    def _queries(self, tab):
        with CaptureQueriesContext(connection) as queries:
            self.get(tab=tab)
        return len(queries)

    def test_query_count_does_not_grow_with_entries_messages_or_files(self):
        # One of each first: the delete right of a file is asked once a file exists.
        post_card_comment(self.card_obj, actor=self.colleague, text='Первое', files=[chat_upload('первый.pdf')])
        legacy_attachment(self.task, self.member, 'первый-задачи.pdf')
        baseline = {tab: self._queries(tab) for tab in self.TABS}
        for index in range(3):
            post_card_comment(
                self.card_obj, actor=self.colleague, text=f'Сообщение {index}',
                files=[chat_upload(f'файл-{index}.pdf'), chat_upload(f'снимок-{index}.png', b'png', 'image/png')],
            )
            legacy_attachment(self.task, self.member, f'файл-задачи-{index}.pdf')
            stage = ('REVIEW', 'TODO', 'IN_PROGRESS')[index]
            move_card(self.card_obj, actor=self.member, column=self.column(stage))
            BoardCardEvent.objects.create(card=self.card_obj, actor=self.member, kind='EDITED', details={})
        for tab in self.TABS:
            self.assertEqual(self._queries(tab), baseline[tab], tab)
        self.assertEqual(len(set(baseline.values())), 1, 'the tab changes no query')
        # Stage 14 brought it from 56 down: the seven live blocks no longer run
        # the context processors (the bell's two queries) each, the open card
        # reuses the исполнители the columns already read, and the rights
        # asked of them come from that same prefetch. No check was removed.
        self.assertEqual(baseline['description'], PAGE_QUERIES)
