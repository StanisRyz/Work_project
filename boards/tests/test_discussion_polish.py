"""«Обсуждение» keeps the unsaved «Выполнение», and a long one stays short."""

import re
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from realtime.fragments import content_revision
from tasks.drafts import EXECUTION_DRAFT_SESSION_KEY
from tasks.models import Task

from ..models import BoardCardComment
from ..selectors import COMMENTS_LIMIT
from ..services import post_card_comment
from .helpers import BoardFixtureMixin


EXECUTION_FIELD = re.compile(r'<textarea id="task-execution-comment"[^>]*>(.*?)</textarea>', re.S)
CARRY_FIELD = 'data-attachment-carry-from="#task-execution-comment"'
CSRF_INPUT = re.compile(r'<input\b[^>]*\bname="csrfmiddlewaretoken"[^>]*>')


def execution_text(response):
    match = EXECUTION_FIELD.search(response.content.decode())
    return match.group(1) if match else None


def attribute(content, name):
    match = re.search(rf'{name}="([^"]*)"', content)
    return match.group(1).replace('&amp;', '&') if match else None


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


class ExecutionDraftTests(BoardFixtureMixin, TestCase):
    """The message form carries «Выполнение» across its round trip."""

    def setUp(self):
        self.card_obj = self.card('Обсуждаемая', assignees=[self.member])
        self.other = self.card('Соседняя', assignees=[self.member])
        self.page_url = reverse('boards:detail', args=[self.board.pk])
        self.client.force_login(self.member)

    def comment_url(self, card):
        return reverse('boards:card_comment', args=[self.board.pk, card.pk])

    def open_panel(self, card):
        return self.client.get(self.page_url, {'card': card.pk})

    def test_the_form_carries_the_field_only_beside_a_completable_task(self):
        content = self.open_panel(self.card_obj).content.decode()
        form = content.split('class="board-discussion__form"', 1)[1].split('</form>', 1)[0]
        self.assertIn('name="execution_comment"', form)
        self.assertIn(CARRY_FIELD, form)
        # A member who is no исполнитель writes messages but completes nothing.
        self.client.force_login(self.colleague)
        content = self.open_panel(self.card_obj).content.decode()
        form = content.split('class="board-discussion__form"', 1)[1].split('</form>', 1)[0]
        self.assertNotIn('name="execution_comment"', form)

    def test_after_a_message_the_execution_field_holds_the_text(self):
        response = self.client.post(
            self.comment_url(self.card_obj),
            {'text': 'Вопрос по сроку', 'execution_comment': 'Наполовину написано'},
            follow=True,
        )
        self.assertEqual(BoardCardComment.objects.get().text, 'Вопрос по сроку')
        self.assertEqual(execution_text(response), 'Наполовину написано')
        self.assertIn('data-panel-holds-input="true"', response.content.decode())
        # A draft, never a result.
        task = task_of(self.card_obj)
        self.assertEqual(task.execution_comment, '')
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        # Shown once: the next visit is the stored state again.
        self.assertEqual(execution_text(self.open_panel(self.card_obj)), '')

    def test_a_refused_message_keeps_the_text_too(self):
        response = self.client.post(
            self.comment_url(self.card_obj), {'text': '   ', 'execution_comment': 'Почти готово'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(execution_text(response), 'Почти готово')
        self.assertFalse(BoardCardComment.objects.exists())

    def test_an_empty_draft_clears_an_old_one(self):
        self.client.post(self.comment_url(self.card_obj), {'text': 'Первое', 'execution_comment': 'Старое'})
        self.assertIn(EXECUTION_DRAFT_SESSION_KEY, self.client.session)
        response = self.client.post(
            self.comment_url(self.card_obj), {'text': 'Второе', 'execution_comment': '  '}, follow=True,
        )
        self.assertNotIn(EXECUTION_DRAFT_SESSION_KEY, self.client.session)
        self.assertEqual(execution_text(response), '')

    def test_a_draft_of_another_task_is_not_inserted(self):
        self.client.post(self.comment_url(self.other), {'text': 'Там', 'execution_comment': 'Чужой черновик'})
        response = self.open_panel(self.card_obj)
        self.assertEqual(execution_text(response), '')
        self.assertNotIn('Чужой черновик', response.content.decode())
        # Left for its own task, which still gets it.
        self.assertEqual(execution_text(self.open_panel(self.other)), 'Чужой черновик')

    def test_a_form_without_the_field_leaves_the_draft_alone(self):
        self.client.post(self.comment_url(self.card_obj), {'text': 'Первое', 'execution_comment': 'Моё'})
        self.client.force_login(self.member)
        self.client.post(self.comment_url(self.card_obj), {'text': 'Без поля'})
        self.assertEqual(execution_text(self.open_panel(self.card_obj)), 'Моё')


class LongDiscussionTests(BoardFixtureMixin, TestCase):
    """The panel reads the newest `COMMENTS_LIMIT` messages; `comments=all` reads every one."""

    LIMIT = 3

    def setUp(self):
        self.card_obj = self.card('Долгая', assignees=[self.member])
        self.page_url = reverse('boards:detail', args=[self.board.pk])
        self.fragment_url = reverse('boards:fragment', args=[self.board.pk])
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
        link = attribute(content, 'class="board-discussion__earlier"><a href')
        self.assertEqual(link, f'{self.page_url}?card={self.card_obj.pk}&comments=all&mine=1')

    def test_all_reads_every_message_and_keeps_asking_for_them(self):
        self.write(5)
        content = self.client.get(
            self.page_url, {'card': self.card_obj.pk, 'comments': 'all', 'mine': '1'},
        ).content.decode()
        self.assertEqual(len(self.comments_of(content)), 5)
        self.assertNotIn('Показать ранние', content)
        query = f'?card={self.card_obj.pk}&comments=all&mine=1'
        self.assertEqual(attribute(content, 'data-board-fragment-url'), self.fragment_url + query)
        self.assertEqual(attribute(content, 'data-board-page-url'), self.page_url + query)

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
                for block in ('columns', 'panel', 'comments'):
                    html = fragment[f'{block}_html']
                    self.assertIn(CSRF_INPUT.sub('', html), CSRF_INPUT.sub('', page))
                    self.assertEqual(attribute(page, f'data-{block}-revision'), fragment[f'{block}_revision'])
                    self.assertEqual(fragment[f'{block}_revision'], content_revision(html))
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
