"""Following a card («Следить»), who hears about a card (`card_audience()`),
and `BOARD_CARD_COMPLETED`."""

from unittest import mock

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification, NotificationDelivery
from notifications.services import EMAIL_ELIGIBLE_EVENTS
from realtime.testing import capture_realtime_events

from ..models import BoardCardEvent, BoardCardSubscription, BoardMember
from ..selectors import card_audience
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    cancel_card,
    complete_card,
    post_card_comment,
    remove_board_member,
    reopen_card,
    toggle_card_subscription,
)
from .helpers import FOLLOW_FORM, BoardFixtureMixin, board_url, fragment_url, make_user
from .test_journal import task_of


def notes(event_type):
    return Notification.objects.filter(event_type=event_type)


def recipients(event_type):
    return sorted(note.recipient.username for note in notes(event_type))


def follows(card, user):
    return BoardCardSubscription.objects.filter(card=card, user=user).exists()


class SubscriptionMixin(BoardFixtureMixin):
    def setUp(self):
        # Author: member; исполнитель: colleague.
        self.card_obj = self.card('Запуск заказа', assignees=[self.colleague], actor=self.member)
        self.follower = make_user('card_follower')
        add_board_members(self.board, [self.follower.pk], actor=self.owner)


class ToggleSubscriptionTests(SubscriptionMixin, TestCase):
    def test_on_and_off_and_the_state_asked_for(self):
        self.assertTrue(toggle_card_subscription(self.card_obj, actor=self.follower))
        self.assertTrue(follows(self.card_obj, self.follower))
        self.assertFalse(toggle_card_subscription(self.card_obj, actor=self.follower))
        self.assertFalse(follows(self.card_obj, self.follower))
        # Asking for the state there is already — a double click — changes nothing.
        self.assertTrue(toggle_card_subscription(self.card_obj, actor=self.follower, subscribe=True))
        self.assertTrue(toggle_card_subscription(self.card_obj, actor=self.follower, subscribe=True))
        self.assertEqual(BoardCardSubscription.objects.filter(card=self.card_obj).count(), 1)
        self.assertFalse(toggle_card_subscription(self.card_obj, actor=self.follower, subscribe=False))
        self.assertFalse(toggle_card_subscription(self.card_obj, actor=self.follower, subscribe=False))

    def test_any_reader_even_of_a_closed_card_but_nobody_else(self):
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Готово')
        # The outsider reads every board in these tests (full access), as an
        # administrator does: a reader, not a member.
        self.assertTrue(toggle_card_subscription(self.card_obj, actor=self.outsider))
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            stranger = make_user('card_stranger')
            with self.assertRaises(BoardError):
                toggle_card_subscription(self.card_obj, actor=stranger)
        self.assertFalse(follows(self.card_obj, stranger))

    def test_an_archived_board_is_refused(self):
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Готово')
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            toggle_card_subscription(self.card_obj, actor=self.follower)

    def test_personal_no_event_and_no_journal(self):
        journal = BoardCardEvent.objects.filter(card=self.card_obj).count()
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                toggle_card_subscription(self.card_obj, actor=self.follower)
                toggle_card_subscription(self.card_obj, actor=self.follower)
            self.assertEqual(publisher.events, [])
        self.assertEqual(BoardCardEvent.objects.filter(card=self.card_obj).count(), journal)

    def test_removing_a_member_removes_their_subscriptions(self):
        other = self.card('Вторая', assignees=[self.colleague])
        toggle_card_subscription(self.card_obj, actor=self.follower)
        toggle_card_subscription(other, actor=self.follower)
        toggle_card_subscription(other, actor=self.member)
        remove_board_member(self.board, self.follower, actor=self.owner)
        self.assertFalse(BoardCardSubscription.objects.filter(user=self.follower).exists())
        self.assertTrue(follows(other, self.member))


class CardAudienceTests(SubscriptionMixin, TestCase):
    def test_assignees_the_author_and_the_followers_each_once(self):
        toggle_card_subscription(self.card_obj, actor=self.follower)
        # The author and an исполнитель follow too: still one each.
        toggle_card_subscription(self.card_obj, actor=self.member)
        toggle_card_subscription(self.card_obj, actor=self.colleague)
        task = task_of(self.card_obj)
        self.assertEqual(
            card_audience(self.card_obj, task), sorted([self.member, self.colleague, self.follower], key=lambda u: u.pk),
        )
        self.assertEqual(card_audience(self.card_obj, task, author=False, subscribers=False), [self.colleague])
        self.assertEqual(
            card_audience(self.card_obj, task, assignees=False),
            sorted([self.member, self.colleague, self.follower], key=lambda u: u.pk),
        )
        self.assertEqual(card_audience(self.card_obj, task, assignees=False, author=False, subscribers=False), [])

    def test_the_inactive_and_those_who_no_longer_read_drop_out(self):
        toggle_card_subscription(self.card_obj, actor=self.follower)
        task = task_of(self.card_obj)
        UserProfile.objects.filter(user=self.member).update(is_active=False)
        self.assertNotIn(self.member, card_audience(self.card_obj, task))
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            # Taken off the board behind the service's back: no longer a reader.
            BoardMember.objects.filter(board=self.board, user=self.follower).delete()
            self.assertEqual(card_audience(self.card_obj, task), [self.colleague])

    def test_one_query(self):
        toggle_card_subscription(self.card_obj, actor=self.follower)
        task = task_of(self.card_obj)
        with self.assertNumQueries(1):
            card_audience(self.card_obj, task)


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class AudienceNoticeTests(SubscriptionMixin, TestCase):
    def setUp(self):
        super().setUp()
        toggle_card_subscription(self.card_obj, actor=self.follower)

    def test_a_message_reaches_the_followers(self):
        post_card_comment(self.card_obj, actor=self.colleague, text='Начал')
        self.assertEqual(
            recipients(Notification.EventType.BOARD_CARD_COMMENT),
            sorted([self.member.username, self.follower.username]),
        )

    def test_a_cancellation_reaches_the_assignees_and_the_followers(self):
        cancel_card(self.card_obj, actor=self.member, reason='Задвоилась')
        self.assertEqual(
            recipients(Notification.EventType.BOARD_TASK_CANCELLED),
            sorted([self.colleague.username, self.follower.username]),
        )

    def test_a_completion_reaches_the_followers_and_the_author_in_the_bell(self):
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Сделано')
        sent = notes(Notification.EventType.BOARD_CARD_COMPLETED)
        self.assertEqual(
            recipients(Notification.EventType.BOARD_CARD_COMPLETED),
            sorted([self.member.username, self.follower.username]),
        )
        note = sent.first()
        task = task_of(self.card_obj)
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertEqual(note.related_task, task)
        self.assertEqual(
            note.deduplication_key,
            f'BOARD_CARD_COMPLETED:task:{task.pk}:completed:{task.completed_at.isoformat()}',
        )
        self.assertEqual(note.title, f'Карточка {self.card_obj.code} выполнена')
        self.assertNotIn('Сделано', note.title + note.message)
        self.assertNotIn(Notification.EventType.BOARD_CARD_COMPLETED, EMAIL_ELIGIBLE_EVENTS)
        self.assertFalse(NotificationDelivery.objects.filter(notification__in=sent).exists())

    def test_completed_again_after_a_reopening_says_so_again(self):
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Сделано')
        reopen_card(self.card_obj, actor=self.admin)
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Теперь точно')
        self.assertEqual(
            notes(Notification.EventType.BOARD_CARD_COMPLETED).filter(recipient=self.follower).count(), 2,
        )

    def test_whoever_completed_it_is_not_told(self):
        toggle_card_subscription(self.card_obj, actor=self.colleague)
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Сделано')
        self.assertNotIn(self.colleague.username, recipients(Notification.EventType.BOARD_CARD_COMPLETED))


class SubscriptionRouteTests(SubscriptionMixin, TestCase):
    def url(self):
        return reverse('boards:card_subscribe', args=[self.board.pk, self.card_obj.pk])

    def panel(self, user):
        self.client.force_login(user)
        return self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()

    def test_the_right_is_asked_before_the_method(self):
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            stranger = make_user('route_stranger')
            self.client.force_login(stranger)
            for method in ('get', 'post'):
                with self.subTest(method=method):
                    self.assertEqual(getattr(self.client, method)(self.url(), {'subscribe': '1'}).status_code, 403)
        self.assertFalse(BoardCardSubscription.objects.exists())

    def test_a_get_changes_nothing_and_a_post_follows(self):
        self.client.force_login(self.follower)
        card_page = f'{board_url(self.board)}?card={self.card_obj.pk}&tab=chat'
        response = self.client.get(self.url(), {'tab': 'chat'})
        self.assertRedirects(response, card_page, fetch_redirect_response=False)
        self.assertFalse(follows(self.card_obj, self.follower))
        response = self.client.post(self.url(), {'subscribe': '1', 'tab': 'chat'})
        self.assertRedirects(response, card_page, fetch_redirect_response=False)
        self.assertTrue(follows(self.card_obj, self.follower))
        self.client.post(self.url(), {'subscribe': '1'})
        self.assertTrue(follows(self.card_obj, self.follower))
        self.client.post(self.url(), {'subscribe': '0'})
        self.assertFalse(follows(self.card_obj, self.follower))

    def test_the_button_and_the_followers_on_the_description(self):
        payload = self.panel(self.follower)
        self.assertIn('>Следить</button>', payload['panel_html'])
        self.assertEqual(payload['followers_html'].strip(), '')
        self.assertNotIn('Подписчики', payload['card_html'] + payload['facts_html'])
        toggle_card_subscription(self.card_obj, actor=self.follower)
        self.follower.first_name, self.follower.last_name = 'Олег', 'Следящий'
        self.follower.save()
        payload = self.panel(self.follower)
        self.assertIn('>Вы следите</button>', payload['panel_html'])
        self.assertIn('aria-pressed="true"', FOLLOW_FORM.search(payload['panel_html']).group(0))
        # «Подписчики» are a read-only block of their own, below the facts.
        self.assertIn('<dt>Подписчики</dt>', payload['followers_html'])
        self.assertIn('title="Олег Следящий">ОС</span>', payload['followers_html'])
        self.assertNotIn('Подписчики', payload['card_html'] + payload['facts_html'])
        drawer = payload['drawer_html']
        self.assertLess(drawer.index('data-live-board-facts'), drawer.index('data-live-board-followers'))
        # Somebody else sees the follower, and their own «Следить».
        payload = self.panel(self.member)
        self.assertIn('>Следить</button>', payload['panel_html'])
        self.assertIn('title="Олег Следящий"', payload['followers_html'])

    def test_a_new_follower_moves_their_block_and_never_the_guarded_panel(self):
        before = self.panel(self.member)
        toggle_card_subscription(self.card_obj, actor=self.follower)
        after = self.panel(self.member)
        self.assertEqual(before['panel_revision'], after['panel_revision'])
        self.assertNotEqual(before['followers_revision'], after['followers_revision'])
        # The colleague a message mentions follows too — and the panel of
        # whoever is reading (perhaps typing a result) stays as it was.
        stranger = make_user('mentioned_follower')
        add_board_members(self.board, [stranger.pk], actor=self.owner)
        post_card_comment(self.card_obj, actor=self.colleague, text='Посмотри', mentions=[stranger.pk])
        mentioned = self.panel(self.member)
        self.assertEqual(after['panel_revision'], mentioned['panel_revision'])
        self.assertNotEqual(after['followers_revision'], mentioned['followers_revision'])

    def test_the_page_carries_the_followers_fingerprint(self):
        toggle_card_subscription(self.card_obj, actor=self.follower)
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.card_obj.pk}).content.decode()
        payload = self.panel(self.member)
        self.assertIn(f'data-followers-revision="{payload["followers_revision"]}"', page)
        self.assertIn(payload['followers_html'], page)

    def test_five_avatars_and_the_rest_counted(self):
        people = [make_user(f'many_follower_{index}') for index in range(7)]
        add_board_members(self.board, [person.pk for person in people], actor=self.owner)
        for person in people:
            toggle_card_subscription(self.card_obj, actor=person)
        html = self.panel(self.member)['followers_html']
        facts = html.split('<dt>Подписчики</dt>', 1)[1].split('</dd>', 1)[0]
        self.assertEqual(facts.count('board-avatar'), 5)
        self.assertIn('+2', facts)

    def test_no_button_on_an_archived_board(self):
        complete_card(self.card_obj, actor=self.colleague, execution_comment='Готово')
        archive_board(self.board, actor=self.owner)
        self.assertIsNone(FOLLOW_FORM.search(self.panel(self.member)['panel_html']))

    def _queries(self):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(board_url(self.board), {'card': self.card_obj.pk})
        return len(queries)

    def test_the_count_does_not_grow_with_followers(self):
        toggle_card_subscription(self.card_obj, actor=self.follower)
        baseline = self._queries()
        people = [make_user(f'count_follower_{index}') for index in range(6)]
        add_board_members(self.board, [person.pk for person in people], actor=self.owner)
        for person in people:
            toggle_card_subscription(self.card_obj, actor=person)
        self.assertEqual(self._queries(), baseline)
