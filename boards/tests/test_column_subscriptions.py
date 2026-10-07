"""Following a column («🔔 Сообщать о новых карточках»,
`toggle_column_subscription()`) and `BOARD_COLUMN_ENTERED`."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification, NotificationDelivery
from notifications.services import EMAIL_ELIGIBLE_EVENTS
from realtime.testing import capture_realtime_events

from ..models import BoardCardEvent, BoardColumnSubscription
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    complete_card,
    create_sub_board,
    move_card,
    remove_board_member,
    toggle_card_subscription,
    toggle_column_subscription,
)
from .helpers import BoardFixtureMixin, board_url, done_column_of, make_user, new_card
from .test_lifecycle import main_of


FETCH = {'HTTP_X_REQUESTED_WITH': 'fetch'}


def entered(**filters):
    return Notification.objects.filter(event_type=Notification.EventType.BOARD_COLUMN_ENTERED, **filters)


def entered_names():
    return sorted(note.recipient.username for note in entered())


class ColumnFixture(BoardFixtureMixin):
    def setUp(self):
        self.master = make_user('shop_master')
        add_board_members(self.board, [self.master.pk], actor=self.owner)
        self.work = self.column('IN_PROGRESS')

    def follows(self, user, column=None):
        return BoardColumnSubscription.objects.filter(column=column or self.work, user=user).exists()


class ToggleColumnSubscriptionTests(ColumnFixture, TestCase):
    def test_on_off_and_the_state_asked_for_with_no_event_and_no_journal(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.assertTrue(toggle_column_subscription(self.work, actor=self.master))
                self.assertTrue(toggle_column_subscription(self.work, actor=self.master, subscribe=True))
            self.assertEqual(publisher.events, [])
        self.assertEqual(BoardColumnSubscription.objects.filter(column=self.work).count(), 1)
        self.assertFalse(toggle_column_subscription(self.work, actor=self.master))
        self.assertFalse(toggle_column_subscription(self.work, actor=self.master, subscribe=False))
        self.assertFalse(self.follows(self.master))
        self.assertFalse(BoardCardEvent.objects.filter(kind__in=['LINKED']).exists())
        # The closing column may be followed too.
        self.assertTrue(toggle_column_subscription(done_column_of(self.board), actor=self.master))

    def test_any_reader_but_never_on_an_archived_board_and_never_a_stranger(self):
        # The outsider reads every board here (full access widened).
        self.assertTrue(toggle_column_subscription(self.work, actor=self.outsider))
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            stranger = make_user('column_stranger')
            with self.assertRaisesMessage(BoardError, 'читатели доски'):
                toggle_column_subscription(self.work, actor=stranger)
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            toggle_column_subscription(self.work, actor=self.master)

    def test_removing_a_member_drops_their_columns(self):
        toggle_column_subscription(self.work, actor=self.master)
        toggle_column_subscription(done_column_of(self.board), actor=self.master)
        remove_board_member(self.board, self.master, actor=self.owner)
        self.assertFalse(BoardColumnSubscription.objects.filter(user=self.master).exists())

    def test_the_menu_offers_it_to_every_reader_and_the_header_shows_the_bell(self):
        self.client.force_login(self.master)
        url = reverse('boards:column_follow', args=[self.board.pk, self.main.pk, self.work.pk])
        content = main_of(self.client.get(board_url(self.board)))
        self.assertIn('🔔 Сообщать о новых карточках', content)
        # A member who manages nothing gets the follow form, and only it.
        self.assertNotIn('Переименовать', content)
        self.assertNotIn('board-column__followed', content)
        response = self.client.post(url, {'subscribe': '1'})
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        self.assertTrue(self.follows(self.master))
        content = main_of(self.client.get(board_url(self.board)))
        self.assertIn('board-column__followed', content)
        self.assertIn('Не сообщать о новых карточках', content)
        self.client.post(url, {'subscribe': '0'})
        self.assertFalse(self.follows(self.master))
        # A GET changes nothing; a stranger is refused before the method.
        self.client.get(url)
        self.assertFalse(self.follows(self.master))
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            self.client.force_login(make_user('column_stranger_view'))
            for method in (self.client.get, self.client.post):
                self.assertEqual(method(url).status_code, 403)


class ColumnEnteredTests(ColumnFixture, TestCase):
    def setUp(self):
        super().setUp()
        toggle_column_subscription(self.work, actor=self.master)

    def test_a_card_created_in_the_column(self):
        self.assertIn(Notification.EventType.BOARD_COLUMN_ENTERED, EMAIL_ELIGIBLE_EVENTS)
        card = self.card('Запуск заказа', stage='IN_PROGRESS')
        note = entered().get()
        self.assertEqual(note.recipient, self.master)
        self.assertEqual(
            note.title, f'Карточка {card.code} вошла в колонку «{self.work.name}» на доске «{self.board.name}»',
        )
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertTrue(NotificationDelivery.objects.filter(notification=note).exists())
        # Created in another column: nothing.
        self.card('Другая', stage='TODO')
        self.assertEqual(entered().count(), 1)

    def test_a_card_moved_in_by_each_of_the_three_paths(self):
        other_tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        foreign = new_card(self.board, self.member, 'С другой вкладки', assignees=[self.member], sub_board=other_tab,
                           column=self.column_in(other_tab))
        first = self.card('Перетаскиванием')
        second = self.card('Через «Переместить в…»')
        self.client.force_login(self.member)
        move_url = lambda card: reverse('boards:card_move', args=[self.board.pk, card.pk])  # noqa: E731
        response = self.client.post(move_url(first), {'column_id': self.work.pk}, **FETCH)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(entered().count(), 1)
        response = self.client.post(move_url(second), {'column_id': self.work.pk})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(entered().count(), 2)
        move_card(foreign, actor=self.member, column=self.work)
        self.assertEqual(entered().count(), 3)
        self.assertEqual(
            sorted(note.related_task.board_card_id for note in entered()), sorted([first.pk, second.pk, foreign.pk]),
        )
        # A reorder inside the column is no entry.
        move_card(first, actor=self.member, column=self.work)
        self.assertEqual(entered().count(), 3)

    def column_in(self, sub_board):
        from ..models import BoardColumn

        return BoardColumn.objects.filter(sub_board=sub_board, is_done=False).order_by('position').first()

    def test_a_card_completed_into_the_closing_column(self):
        done = done_column_of(self.board)
        toggle_column_subscription(done, actor=self.master)
        card = self.card('К завершению')
        complete_card(card, actor=self.member, execution_comment='Готово')
        note = entered().get()
        self.assertIn(f'«{done.name}»', note.title)

    def test_not_to_whoever_moved_it(self):
        toggle_column_subscription(self.work, actor=self.member)
        self.card('Своя', stage='IN_PROGRESS', actor=self.member, assignees=[self.colleague])
        self.assertEqual(entered_names(), ['shop_master'])

    def test_one_notification_per_person_for_one_action(self):
        # An исполнитель who follows the column hears «назначена», not both.
        toggle_column_subscription(self.work, actor=self.colleague)
        self.card('Назначенная', stage='IN_PROGRESS', assignees=[self.colleague])
        self.assertEqual(entered_names(), ['shop_master'])
        self.assertEqual(
            Notification.objects.filter(recipient=self.colleague).count(), 1,
        )
        # A follower of the card and of the closing column hears the
        # completion once.
        done = done_column_of(self.board)
        toggle_column_subscription(done, actor=self.colleague)
        card = self.card('Под наблюдением')
        toggle_card_subscription(card, actor=self.colleague)
        before = Notification.objects.filter(recipient=self.colleague).count()
        complete_card(card, actor=self.member, execution_comment='Готово')
        self.assertEqual(Notification.objects.filter(recipient=self.colleague).count(), before + 1)
        self.assertFalse(entered(recipient=self.colleague, related_task__board_card=card).exists())

    def test_never_to_somebody_who_does_not_read_the_board(self):
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            stranger = make_user('column_reader_gone')
            # A row left behind by hand: the reader filter still holds.
            BoardColumnSubscription.objects.create(column=self.work, user=stranger)
            self.card('Ещё одна', stage='IN_PROGRESS')
        self.assertEqual(entered_names(), ['shop_master'])

    def test_a_subtask_enters_no_column(self):
        from ..services import create_subtask

        card = self.card('Карточка', stage='IN_PROGRESS')
        create_subtask(card, actor=self.member, title='Подзадача', assignees=[self.member])
        self.assertEqual(entered().count(), 1)
