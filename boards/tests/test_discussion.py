"""Stage 7: «Обсуждение» in the card panel, and the cancellation notice."""

import json

from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification, NotificationDelivery
from notifications.services import EMAIL_ELIGIBLE_EVENTS, get_notification_url
from realtime.events import RealtimeEventType
from realtime.fragments import content_revision
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.services import complete_task

from ..models import BoardCardComment
from ..permissions import can_comment_card
from ..services import (
    COMMENT_MAX_LENGTH,
    BoardError,
    archive_board,
    cancel_card,
    post_card_comment,
)
from .helpers import (
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    fragment_url,
    make_user,
)


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


def notes(event_type):
    return Notification.objects.filter(event_type=event_type)


def main_of(response):
    return response.content.decode().split('<main', 1)[1].split('</main>', 1)[0]


# --------------------------------------------------------------------------
# §0 — the cancellation notice
# --------------------------------------------------------------------------


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class CancellationNoticeTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        # Put on both members by the member, who then cancels it.
        self.card_obj = self.card('Отменяемая', assignees=[self.member, self.colleague], actor=self.member)

    def test_the_assignees_hear_of_it_and_the_canceller_does_not(self):
        cancel_card(self.card_obj, actor=self.member, reason='Задвоилась')
        sent = notes(Notification.EventType.BOARD_TASK_CANCELLED)
        self.assertEqual([note.recipient for note in sent], [self.colleague])
        note = sent.get()
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertEqual(note.related_task, task_of(self.card_obj))
        self.assertIn('Планирование', note.title)
        self.assertNotIn('Задвоилась', note.title + note.message)
        self.assertEqual(note.deduplication_key, f'BOARD_TASK_CANCELLED:task:{note.related_task_id}:cancelled')
        self.assertNotIn(Notification.EventType.BOARD_TASK_CANCELLED, EMAIL_ELIGIBLE_EVENTS)
        self.assertFalse(NotificationDelivery.objects.filter(notification=note).exists())

    def test_a_rollback_leaves_no_notice(self):
        try:
            with transaction.atomic():
                cancel_card(self.card_obj, actor=self.member, reason='Задвоилась')
                raise RuntimeError('rollback')
        except RuntimeError:
            pass
        self.assertFalse(notes(Notification.EventType.BOARD_TASK_CANCELLED).exists())
        self.assertEqual(task_of(self.card_obj).status.code, 'IN_PROGRESS')

    def test_a_refusal_leaves_no_notice(self):
        with self.assertRaises(BoardError):
            cancel_card(self.card_obj, actor=self.member, reason='  ')
        self.assertFalse(notes(Notification.EventType.BOARD_TASK_CANCELLED).exists())


# --------------------------------------------------------------------------
# Writing a message
# --------------------------------------------------------------------------


class PostCommentTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Обсуждаемая', assignees=[self.colleague], actor=self.member)

    def test_who_may_write(self):
        card = self.card_obj
        self.assertTrue(can_comment_card(self.member, card))
        self.assertTrue(can_comment_card(self.admin, card))
        self.assertFalse(can_comment_card(self.outsider, card))
        for user in (self.member, self.admin):
            post_card_comment(card, actor=user, text=f'Пишет {user.username}')
        with self.assertRaises(BoardError):
            post_card_comment(card, actor=self.outsider, text='Читатель')
        self.assertEqual(BoardCardComment.objects.filter(card=card).count(), 2)

    def test_text_is_stripped_required_and_bounded(self):
        comment = post_card_comment(self.card_obj, actor=self.member, text='  Привет  ')
        self.assertEqual(comment.text, 'Привет')
        for text in ('', '   ', 'я' * (COMMENT_MAX_LENGTH + 1)):
            with self.subTest(length=len(text)):
                with self.assertRaises(BoardError):
                    post_card_comment(self.card_obj, actor=self.member, text=text)
        post_card_comment(self.card_obj, actor=self.member, text='я' * COMMENT_MAX_LENGTH)
        self.assertEqual(BoardCardComment.objects.count(), 2)

    def test_a_completed_and_a_cancelled_card_are_still_discussed(self):
        done = self.card('Готовая', assignees=[self.member])
        complete_task(task_of(done), self.member, 'Да')
        cancelled = self.card('Отменённая', assignees=[self.member])
        cancel_card(cancelled, actor=self.member, reason='Не нужна')
        post_card_comment(done, actor=self.colleague, text='Принято')
        post_card_comment(cancelled, actor=self.colleague, text='Почему?')
        self.assertEqual(BoardCardComment.objects.count(), 2)

    def test_an_archived_board_takes_no_message(self):
        complete_task(task_of(self.card_obj), self.colleague, 'Да')
        archive_board(self.board, actor=self.owner)
        self.card_obj.refresh_from_db()
        self.assertFalse(can_comment_card(self.admin, self.card_obj))
        with self.assertRaises(BoardError):
            post_card_comment(self.card_obj, actor=self.admin, text='В архив?')

    def test_one_event_after_commit_and_none_on_refusal(self):
        token = build_sync_state(self.outsider)['revisions'][REVISION_BOARDS]
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                post_card_comment(self.card_obj, actor=self.member, text='Секретный текст')
                self.assertEqual(board_events(publisher), [])
            events = board_events(publisher)
            self.assertEqual(
                [(event.data['change'], event.data['card_id']) for event in events],
                [('comment_added', self.card_obj.pk)],
            )
            for event in publisher.events:
                self.assertNotIn('Секретный', json.dumps(event.as_dict(), ensure_ascii=False))
        self.assertNotEqual(build_sync_state(self.outsider)['revisions'][REVISION_BOARDS], token)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(BoardError):
                    post_card_comment(self.card_obj, actor=self.outsider, text='Нельзя')
            self.assertEqual(publisher.events, [])


# --------------------------------------------------------------------------
# Notifications of a message
# --------------------------------------------------------------------------


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class CommentNoticeTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        # Author: member; исполнители: colleague and a third member.
        self.third = make_user('discussion_third')
        from ..services import add_board_members
        add_board_members(self.board, [self.third.pk], actor=self.owner)
        self.card_obj = self.card('С обсуждением', assignees=[self.colleague, self.third], actor=self.member)

    def recipients(self):
        return sorted(
            note.recipient.username
            for note in notes(Notification.EventType.BOARD_CARD_COMMENT)
        )

    def test_assignees_and_the_author_except_the_writer(self):
        comment = post_card_comment(self.card_obj, actor=self.colleague, text='Начал')
        self.assertEqual(self.recipients(), sorted([self.member.username, self.third.username]))
        note = notes(Notification.EventType.BOARD_CARD_COMMENT).first()
        self.assertEqual(note.deduplication_key, f'BOARD_CARD_COMMENT:board_comment:{comment.pk}')
        self.assertEqual(note.related_task, task_of(self.card_obj))
        self.assertIn('Планирование', note.title)
        self.assertNotIn('Начал', note.title + note.message)
        self.assertNotIn(Notification.EventType.BOARD_CARD_COMMENT, EMAIL_ELIGIBLE_EVENTS)
        self.assertFalse(NotificationDelivery.objects.filter(
            notification__event_type=Notification.EventType.BOARD_CARD_COMMENT,
        ).exists())

    def test_one_notice_per_message_and_none_for_inactive(self):
        UserProfile.objects.filter(user=self.third).update(is_active=False)
        post_card_comment(self.card_obj, actor=self.admin, text='Первое')
        post_card_comment(self.card_obj, actor=self.admin, text='Второе')
        self.assertEqual(
            self.recipients(),
            sorted([self.member.username, self.colleague.username] * 2),
        )

    def test_the_link_opens_the_card(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='Ссылка')
        note = notes(Notification.EventType.BOARD_CARD_COMMENT).first()
        self.assertEqual(
            get_notification_url(note), reverse('tasks:detail', args=[note.related_task_id]),
        )


# --------------------------------------------------------------------------
# The route, the page and the live blocks
# --------------------------------------------------------------------------


class DiscussionPageTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Обсуждаемая', assignees=[self.member])
        self.url = reverse('boards:card_comment', args=[self.board.pk, self.card_obj.pk])
        self.page_url = board_url(self.board)

    def test_route_asks_before_the_method_and_get_changes_nothing(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, {'text': 'x'}).status_code, 403)
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertRedirects(response, f'{self.page_url}?card={self.card_obj.pk}', fetch_redirect_response=False)
        self.assertFalse(BoardCardComment.objects.exists())

    def test_success_and_refusal(self):
        self.client.force_login(self.member)
        response = self.client.post(f'{self.url}?mine=1', {'text': 'Готов к работе'})
        self.assertRedirects(
            response, f'{self.page_url}?card={self.card_obj.pk}&tab=chat&mine=1', fetch_redirect_response=False,
        )
        self.assertEqual(BoardCardComment.objects.get().text, 'Готов к работе')
        response = self.client.post(self.url, {'text': '   '})
        self.assertEqual(response.status_code, 200)
        content = main_of(response)
        self.assertIn('Напишите сообщение.', content)
        self.assertIn('data-unsaved-guard="dirty"', content)

    def test_the_panel_shows_the_discussion_and_the_form_only_to_writers(self):
        post_card_comment(self.card_obj, actor=self.member, text='Первое\nвторая строка')
        self.client.force_login(self.colleague)
        content = main_of(self.client.get(self.page_url, {'card': self.card_obj.pk}))
        self.assertIn('>Чат <span class="tab-count" data-board-tab-count="chat">1</span>', content)
        self.assertIn('Первое<br>вторая строка', content)
        self.assertIn('class="board-discussion__text user-text"', content)
        self.assertIn(f'action="{self.url}"', content)
        self.client.force_login(self.outsider)
        content = main_of(self.client.get(self.page_url, {'card': self.card_obj.pk}))
        self.assertIn('Первое', content)
        self.assertNotIn(f'action="{self.url}"', content)

    def test_tile_counter(self):
        self.client.force_login(self.member)
        content = main_of(self.client.get(self.page_url))
        self.assertNotIn('board-tile__comments', content)
        post_card_comment(self.card_obj, actor=self.member, text='1')
        post_card_comment(self.card_obj, actor=self.member, text='2')
        content = main_of(self.client.get(self.page_url))
        self.assertIn('Сообщений в обсуждении: 2', content)

    def test_fragment_equals_the_page_for_every_block(self):
        post_card_comment(self.card_obj, actor=self.member, text='Есть')
        self.client.force_login(self.member)
        query = {'card': self.card_obj.pk}
        page = self.client.get(self.page_url, query).content.decode()
        fragment = self.client.get(fragment_url(self.board), query).json()
        assert_page_matches_fragment(self, page, fragment)
        for block in ('columns', 'comments', 'log'):
            self.assertEqual(fragment[f'{block}_revision'], content_revision(fragment[f'{block}_html']))
        self.assertEqual(
            fragment['panel_revision'], content_revision(fragment['panel_html'] + fragment['card_html']),
        )
        self.assertNotIn('Есть', fragment['panel_html'] + fragment['card_html'])
        self.assertEqual(fragment['chat_count'], 1)

    def test_a_new_message_does_not_move_the_panel_fingerprint(self):
        self.client.force_login(self.member)
        url = fragment_url(self.board)
        before = self.client.get(url, {'card': self.card_obj.pk}).json()
        post_card_comment(self.card_obj, actor=self.colleague, text='Новое')
        after = self.client.get(url, {'card': self.card_obj.pk}).json()
        self.assertEqual(before['panel_revision'], after['panel_revision'])
        self.assertNotEqual(before['comments_revision'], after['comments_revision'])
        self.assertNotEqual(before['columns_revision'], after['columns_revision'], 'счётчик на плитке')
        self.assertIn('Новое', after['comments_html'])

    def test_no_chat_without_a_card(self):
        self.client.force_login(self.member)
        url = fragment_url(self.board)
        column = self.column('TODO').pk
        for query in ({}, {'new': column}, {'new': 'TODO'}):
            with self.subTest(query=query):
                payload = self.client.get(url, query).json()
                self.assertEqual(payload['comments_html'], '')
                self.assertEqual(payload['comments_revision'], '')
                self.assertEqual(payload['log_html'], '')
        # Editing a card keeps its chat and its log beside the form.
        payload = self.client.get(url, {'card': self.card_obj.pk, 'edit': '1'}).json()
        self.assertTrue(payload['comments_html'])
        self.assertTrue(payload['log_html'])

    def _queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.page_url, {'card': self.card_obj.pk})
        return len(queries)

    def test_query_count_is_constant(self):
        self.client.force_login(self.member)
        post_card_comment(self.card_obj, actor=self.member, text='Одно')
        baseline = self._queries()
        for index in range(4):
            post_card_comment(self.card_obj, actor=self.colleague, text=f'Ещё {index}')
            other = self.card(f'Карточка {index}', assignees=[self.member, self.colleague])
            post_card_comment(other, actor=self.member, text='Там тоже')
        self.assertEqual(self._queries(), baseline)
