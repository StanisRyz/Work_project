"""«Чат» carries no «Выполнение» draft any more, and a long one stays short."""

import re
import tempfile
from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from realtime.fragments import content_revision
from tasks.drafts import EXECUTION_DRAFT_SESSION_KEY
from tasks.models import Task, TaskAttachment

from ..models import BoardCardComment
from ..selectors import COMMENTS_LIMIT
from ..services import post_card_comment
from .helpers import (
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    fragment_url,
    legacy_attachment,
    page_attribute,
)


RESULT_FIELD = re.compile(r'<textarea id="board-complete-result"[^>]*>(.*?)</textarea>', re.S)


def result_text(response):
    match = RESULT_FIELD.search(response.content.decode())
    return match.group(1) if match else None


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


class NoExecutionDraftTests(BoardFixtureMixin, TestCase):
    """The board neither carries nor takes a «Выполнение» draft (stage 13).

    The result is typed into «Завершить» in the drawer's heading; the chat's
    form and the attachment forms post nothing on its behalf, and nothing a
    session holds is put into the field.
    """

    def setUp(self):
        self.card_obj = self.card('Обсуждаемая', assignees=[self.member])
        self.page_url = board_url(self.board)
        self.client.force_login(self.member)

    def comment_url(self):
        return reverse('boards:card_comment', args=[self.board.pk, self.card_obj.pk])

    def open_panel(self, **extra):
        return self.client.get(self.page_url, {'card': self.card_obj.pk, **extra})

    def test_the_message_form_carries_no_execution_field(self):
        content = self.open_panel().content.decode()
        form = content.split('class="board-chat__form"', 1)[1].split('</form>', 1)[0]
        self.assertIn('name="text"', form)
        self.assertNotIn('execution_comment', form)
        self.assertNotIn('data-attachment-carry-from', form)
        # No «Выполнение» textarea on the board to carry from.
        self.assertNotIn('id="task-execution-comment"', content)
        self.assertEqual(result_text(self.open_panel()), '')

    def test_a_message_parks_nothing(self):
        response = self.client.post(
            self.comment_url(), {'text': 'Вопрос по сроку', 'execution_comment': 'Наполовину написано'},
            follow=True,
        )
        self.assertEqual(BoardCardComment.objects.get().text, 'Вопрос по сроку')
        self.assertNotIn(EXECUTION_DRAFT_SESSION_KEY, self.client.session)
        self.assertEqual(result_text(response), '')
        self.assertNotIn('Наполовину написано', response.content.decode())
        self.assertIn('data-panel-holds-input="false"', response.content.decode())
        # The message lands on «Чат».
        self.assertEqual(response.redirect_chain[-1][0], f'{self.page_url}?card={self.card_obj.pk}&tab=chat')

    def test_the_panel_takes_no_draft_from_the_session(self):
        session = self.client.session
        session[EXECUTION_DRAFT_SESSION_KEY] = {'task': task_of(self.card_obj).pk, 'text': 'Старый черновик'}
        session.save()
        response = self.open_panel()
        self.assertEqual(result_text(response), '')
        self.assertNotIn('Старый черновик', response.content.decode())

    @override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='board-files-'))
    def test_an_older_attachment_is_removed_from_the_chat_without_a_draft(self):
        task = task_of(self.card_obj)
        attachment = legacy_attachment(task, self.member)
        content = self.open_panel(tab='chat').content.decode()
        form = content.split(f'id="board-attachment-delete-{attachment.pk}"', 1)[1].split('</form>', 1)[0]
        # The chat's own form for it: back to «Чат», nothing to carry.
        self.assertIn('name="list_query" value="tab=chat"', form)
        self.assertNotIn('execution_comment', form)
        response = self.client.post(
            reverse('tasks:delete_attachment', args=[task.pk, attachment.pk]),
            {'list_query': 'tab=chat'}, follow=True,
        )
        self.assertFalse(TaskAttachment.objects.filter(task=task).exists())
        self.assertEqual(
            response.redirect_chain[-1][0], f'{self.page_url}?card={self.card_obj.pk}&tab=chat',
        )
        self.assertIn('data-board-tab="chat"', response.content.decode())
        self.assertNotIn(EXECUTION_DRAFT_SESSION_KEY, self.client.session)


class LongDiscussionTests(BoardFixtureMixin, TestCase):
    """The panel reads the newest `COMMENTS_LIMIT` messages; `comments=all` reads every one."""

    LIMIT = 3

    def setUp(self):
        self.card_obj = self.card('Долгая', assignees=[self.member])
        self.page_url = board_url(self.board)
        self.fragment_url = fragment_url(self.board)
        self.client.force_login(self.member)
        patcher = mock.patch('boards.selectors.COMMENTS_LIMIT', self.LIMIT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, count, card=None):
        for index in range(count):
            post_card_comment(card or self.card_obj, actor=self.colleague, text=f'Сообщение {index + 1:03}')

    def comments_of(self, content):
        return re.findall(r'Сообщение \d{3}', content)

    def test_the_limit_is_a_hundred(self):
        self.assertEqual(COMMENTS_LIMIT, 100)

    def test_the_newest_are_shown_oldest_first_and_the_rest_counted(self):
        self.write(5)
        content = self.client.get(self.page_url, {'card': self.card_obj.pk, 'mine': '1'}).content.decode()
        self.assertEqual(self.comments_of(content), ['Сообщение 003', 'Сообщение 004', 'Сообщение 005'])
        self.assertIn('Показать ранние (2)', content)
        link = page_attribute(content, 'class="board-discussion__earlier"><a href')
        self.assertEqual(link, f'{self.page_url}?card={self.card_obj.pk}&comments=all&tab=chat&mine=1')

    def test_all_reads_every_message_and_keeps_asking_for_them(self):
        self.write(5)
        content = self.client.get(
            self.page_url, {'card': self.card_obj.pk, 'comments': 'all', 'mine': '1'},
        ).content.decode()
        self.assertEqual(len(self.comments_of(content)), 5)
        self.assertNotIn('Показать ранние', content)
        query = f'?card={self.card_obj.pk}&comments=all&tab=description&mine=1'
        self.assertEqual(page_attribute(content, 'data-board-fragment-url'), self.fragment_url + query)
        self.assertEqual(page_attribute(content, 'data-board-page-url'), self.page_url + query)

    def test_no_link_while_everything_fits(self):
        self.write(self.LIMIT)
        content = self.client.get(self.page_url, {'card': self.card_obj.pk}).content.decode()
        self.assertEqual(len(self.comments_of(content)), self.LIMIT)
        self.assertNotIn('Показать ранние', content)

    def test_fragment_equals_the_page_with_all(self):
        self.write(5)
        for query in ({'card': self.card_obj.pk}, {'card': self.card_obj.pk, 'comments': 'all'}):
            with self.subTest(query=query):
                page = self.client.get(self.page_url, query).content.decode()
                fragment = self.client.get(self.fragment_url, query).json()
                assert_page_matches_fragment(self, page, fragment)
                self.assertEqual(fragment['comments_revision'], content_revision(fragment['comments_html']))
        limited = self.client.get(self.fragment_url, {'card': self.card_obj.pk}).json()
        every = self.client.get(self.fragment_url, {'card': self.card_obj.pk, 'comments': 'all'}).json()
        self.assertEqual(len(self.comments_of(limited['comments_html'])), self.LIMIT)
        self.assertEqual(len(self.comments_of(every['comments_html'])), 5)
        self.assertEqual(limited['panel_revision'], every['panel_revision'])

    def _queries(self, **extra):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.page_url, {'card': self.card_obj.pk, **extra})
        return len(queries)

    def test_query_count_does_not_grow_with_the_messages(self):
        self.write(1)
        baseline, baseline_all = self._queries(), self._queries(comments='all')
        self.write(12)
        self.write(4, card=self.card('Другая', assignees=[self.member, self.colleague]))
        self.assertEqual(self._queries(), baseline)
        self.assertEqual(self._queries(comments='all'), baseline_all)
        self.assertEqual(baseline, baseline_all)
