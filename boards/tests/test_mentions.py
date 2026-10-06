"""«@» in a card's «Чат»: who may be mentioned, what is stored, who is told
and how a message reads."""

from unittest import mock

from django.db import connection
from django.template import Context, Template
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification, NotificationDelivery
from notifications.services import EMAIL_ELIGIBLE_EVENTS, get_notification_url

from ..models import BoardCardComment, BoardCardCommentMention, BoardCardSubscription
from ..services import add_board_members, complete_card, post_card_comment
from .helpers import BoardFixtureMixin, board_url, fragment_url, make_user


def notes(event_type):
    return Notification.objects.filter(event_type=event_type)


def recipients(event_type):
    return sorted(note.recipient.username for note in notes(event_type))


def mentioned(comment):
    return sorted(BoardCardCommentMention.objects.filter(comment=comment).values_list('user__username', flat=True))


def named(user, first, last):
    user.first_name, user.last_name = first, last
    user.save()
    return user


class MentionMixin(BoardFixtureMixin):
    def setUp(self):
        # Author: member; исполнитель: colleague.
        self.card_obj = self.card('Запуск заказа', assignees=[self.colleague], actor=self.member)
        self.reader = make_user('mention_reader')
        add_board_members(self.board, [self.reader.pk], actor=self.owner)
        named(self.reader, 'Ирина', 'Читающая')
        named(self.colleague, 'Пётр', 'Исполнитель')


class MentionServiceTests(MentionMixin, TestCase):
    def test_the_readers_are_stored_and_anything_else_dropped(self):
        inactive = make_user('mention_inactive')
        add_board_members(self.board, [inactive.pk], actor=self.owner)
        UserProfile.objects.filter(user=inactive).update(is_active=False)
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            stranger = make_user('mention_stranger')
            comment = post_card_comment(
                self.card_obj, actor=self.member, text='@Ирина Читающая, посмотрите',
                mentions=[
                    str(self.reader.pk), self.reader.pk, 'abc', '', None, str(stranger.pk), inactive.pk,
                    self.member.pk, 999999, str(self.admin.pk),
                ],
            )
        # A reader, and an administrator (full access); never the writer, a
        # stranger, an inactive account, a repeat or a malformed id.
        self.assertEqual(mentioned(comment), sorted([self.reader.username, self.admin.username]))

    def test_the_mentioned_follow_the_card(self):
        BoardCardSubscription.objects.create(card=self.card_obj, user=self.admin)
        post_card_comment(self.card_obj, actor=self.member, text='@всем', mentions=[self.reader.pk, self.admin.pk])
        self.assertEqual(
            sorted(BoardCardSubscription.objects.filter(card=self.card_obj).values_list('user__username', flat=True)),
            sorted([self.reader.username, self.admin.username]),
        )
        # Following from now on: the next message reaches them too.
        post_card_comment(self.card_obj, actor=self.colleague, text='Второе')
        self.assertIn(self.reader.username, recipients(Notification.EventType.BOARD_CARD_COMMENT))

    def test_a_refused_message_stores_no_mention(self):
        from ..services import BoardError

        with self.assertRaises(BoardError):
            post_card_comment(self.card_obj, actor=self.member, text='  ', mentions=[self.reader.pk])
        self.assertFalse(BoardCardCommentMention.objects.exists())
        self.assertFalse(BoardCardSubscription.objects.exists())
        self.assertFalse(notes(Notification.EventType.BOARD_CARD_MENTION).exists())


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class MentionNoticeTests(MentionMixin, TestCase):
    def setUp(self):
        super().setUp()
        for user in (self.reader, self.colleague, self.member):
            user.email = f'{user.username}@plant.test'
            user.save()

    def test_one_notice_per_person_per_message(self):
        comment = post_card_comment(
            self.card_obj, actor=self.member, text='@Пётр Исполнитель и @Ирина Читающая',
            mentions=[self.colleague.pk, self.reader.pk],
        )
        # The mentioned исполнитель is told once — by the mention.
        self.assertEqual(
            recipients(Notification.EventType.BOARD_CARD_MENTION),
            sorted([self.colleague.username, self.reader.username]),
        )
        self.assertEqual(recipients(Notification.EventType.BOARD_CARD_COMMENT), [])
        self.assertFalse(Notification.objects.filter(
            recipient=self.member,
            event_type__in=[Notification.EventType.BOARD_CARD_COMMENT, Notification.EventType.BOARD_CARD_MENTION],
        ).exists())
        note = notes(Notification.EventType.BOARD_CARD_MENTION).get(recipient=self.reader)
        self.assertEqual(note.deduplication_key, f'BOARD_CARD_MENTION:board_comment:{comment.pk}')
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertEqual(note.title, f'Вас упомянули в карточке {self.card_obj.code} на доске «Планирование»')
        self.assertNotIn('Ирина', note.title + note.message)

    def test_the_writer_is_told_nothing_and_the_others_get_the_message(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='@Ирина Читающая', mentions=[self.reader.pk])
        self.assertEqual(recipients(Notification.EventType.BOARD_CARD_MENTION), [self.reader.username])
        self.assertEqual(recipients(Notification.EventType.BOARD_CARD_COMMENT), [self.member.username])
        self.assertFalse(Notification.objects.filter(
            recipient=self.colleague,
            event_type__in=[Notification.EventType.BOARD_CARD_COMMENT, Notification.EventType.BOARD_CARD_MENTION],
        ).exists())

    def test_a_mention_is_mailed_a_completion_is_not(self):
        self.assertIn(Notification.EventType.BOARD_CARD_MENTION, EMAIL_ELIGIBLE_EVENTS)
        post_card_comment(self.card_obj, actor=self.member, text='@Ирина Читающая', mentions=[self.reader.pk])
        note = notes(Notification.EventType.BOARD_CARD_MENTION).get()
        delivery = NotificationDelivery.objects.get(notification=note)
        self.assertEqual(delivery.status, NotificationDelivery.Status.PENDING)
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Готово')
        completed = notes(Notification.EventType.BOARD_CARD_COMPLETED)
        self.assertTrue(completed.exists())
        self.assertFalse(NotificationDelivery.objects.filter(notification__in=completed).exists())

    def test_the_bell_and_the_mail_open_the_cards_chat(self):
        post_card_comment(self.card_obj, actor=self.member, text='@Ирина Читающая', mentions=[self.reader.pk])
        note = notes(Notification.EventType.BOARD_CARD_MENTION).get()
        url = get_notification_url(note)
        self.assertEqual(url, reverse('tasks:detail', args=[note.related_task_id]) + '?tab=chat')
        self.assertTrue(get_notification_url(note, absolute=True).endswith('?tab=chat'))
        self.client.force_login(self.reader)
        response = self.client.get(url)
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.card_obj.pk}&tab=chat', fetch_redirect_response=False,
        )


class MentionDisplayTests(MentionMixin, TestCase):
    def render(self, comment):
        comment = BoardCardComment.objects.prefetch_related('mentions__user').get(pk=comment.pk)
        return Template('{% load board_text %}{{ comment|with_mentions }}').render(Context({'comment': comment}))

    def test_the_mentioned_names_are_highlighted_nothing_else(self):
        named(self.admin, 'Ирина', 'Читающая-Старшая')
        comment = post_card_comment(
            self.card_obj, actor=self.member,
            text='@Ирина Читающая, а @Пётр Исполнитель не упомянут.\n@Ирина Читающая-Старшая тоже',
            mentions=[self.reader.pk, self.admin.pk],
        )
        html = self.render(comment)
        self.assertEqual(html.count('<span class="board-mention">@Ирина Читающая</span>'), 1)
        self.assertIn('<span class="board-mention">@Ирина Читающая-Старшая</span>', html)
        self.assertIn('а @Пётр Исполнитель не упомянут.<br>', html)

    def test_markup_in_the_message_and_in_a_name_stays_text(self):
        named(self.reader, '<script>alert(1)</script>', 'Злой')
        comment = post_card_comment(
            self.card_obj, actor=self.member,
            text='<script>alert(2)</script> @<script>alert(1)</script> Злой <b>жирный</b>',
            mentions=[self.reader.pk],
        )
        html = self.render(comment)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<b>', html)
        self.assertIn('&lt;script&gt;alert(2)&lt;/script&gt;', html)
        self.assertIn(
            '<span class="board-mention">@&lt;script&gt;alert(1)&lt;/script&gt; Злой</span>', html,
        )

    def test_the_chat_draws_it_and_the_form_offers_the_readers(self):
        post_card_comment(self.card_obj, actor=self.member, text='@Ирина Читающая', mentions=[self.reader.pk])
        self.client.force_login(self.colleague)
        payload = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk, 'tab': 'chat'}).json()
        self.assertIn('<span class="board-mention">@Ирина Читающая</span>', payload['comments_html'])
        drawer = payload['drawer_html']
        self.assertIn('data-board-mentions', drawer)
        self.assertIn('<summary>Упомянуть</summary>', drawer)
        self.assertIn(f'name="mention" value="{self.reader.pk}" data-mention-name="Ирина Читающая"', drawer)
        # Not the writer themselves.
        self.assertNotIn(f'name="mention" value="{self.colleague.pk}"', drawer)

    def test_the_route_posts_the_mentions_and_a_refusal_keeps_them(self):
        self.client.force_login(self.member)
        url = reverse('boards:card_comment', args=[self.board.pk, self.card_obj.pk])
        response = self.client.post(url, {'text': '@Ирина Читающая, глянь', 'mention': [self.reader.pk]})
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.card_obj.pk}&tab=chat', fetch_redirect_response=False,
        )
        self.assertEqual(mentioned(BoardCardComment.objects.get()), [self.reader.username])
        response = self.client.post(url, {'text': '   ', 'mention': [self.reader.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            f'name="mention" value="{self.reader.pk}" data-mention-name="Ирина Читающая" checked',
            response.content.decode(),
        )

    def _queries(self):
        self.client.force_login(self.colleague)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(fragment_url(self.board), {'card': self.card_obj.pk, 'tab': 'chat'})
        return len(queries)

    def test_the_count_does_not_grow_with_mentions(self):
        post_card_comment(self.card_obj, actor=self.member, text='@Ирина Читающая', mentions=[self.reader.pk])
        baseline = self._queries()
        people = [make_user(f'mention_many_{index}') for index in range(5)]
        add_board_members(self.board, [person.pk for person in people], actor=self.owner)
        for index in range(4):
            post_card_comment(
                self.card_obj, actor=self.member, text=f'Сообщение {index}',
                mentions=[person.pk for person in people] + [self.reader.pk],
            )
        self.assertEqual(self._queries(), baseline)
