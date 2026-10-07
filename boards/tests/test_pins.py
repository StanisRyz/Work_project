"""Stage 14: «Закреплённые исполнители» of a column, and moving a card to
another sub-board of its board.

A pin acts on a card created in the column or moved into it — by a drag, by
«Переместить в…», or from another sub-board — and never on the cards already
standing there. Only active members count, `ADD` adds, `REPLACE` replaces
unless it would leave nobody, the people added are told once, and the journal
says «Исполнители по колонке «…»».
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task

from ..models import BoardCardEvent, BoardColumn, BoardColumnPin
from ..selectors import describe_card_event
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    create_sub_board,
    move_card,
    remove_board_member,
    set_column_pins,
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    column_of,
    done_column_of,
    fragment_url,
    make_user,
    new_card,
)


FETCH = {'HTTP_X_REQUESTED_WITH': 'fetch'}
ADD = BoardColumn.PinnedMode.ADD
REPLACE = BoardColumn.PinnedMode.REPLACE


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def assignees(card):
    return sorted(task_of(card).assignees.values_list('user__username', flat=True))


def told(user):
    """How many «назначена карточка» `user` has had."""
    return Notification.objects.filter(
        event_type=Notification.EventType.BOARD_TASK_ASSIGNED, recipient=user,
    ).count()


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


class PinsFixture(BoardFixtureMixin):
    """The fixture board plus a launcher member pinned to «В работе»."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.launcher = make_user('launcher', UserProfile.Role.PDO)
        add_board_members(cls.board, [cls.launcher.pk], actor=cls.owner)

    def pin(self, stage='IN_PROGRESS', users=None, mode=ADD, actor=None):
        column = self.column(stage)
        set_column_pins(
            column, actor=actor or self.owner, mode=mode,
            user_ids=[user.pk for user in (users if users is not None else [self.launcher])],
        )
        return column

    def pin_entries(self, card):
        return list(
            BoardCardEvent.objects.filter(card=card, kind=BoardCardEvent.Kind.EDITED, details__has_key='by_column')
        )


class SetPinsTests(PinsFixture, TestCase):
    def test_owner_and_administrator_set_them_once(self):
        for actor, mode in ((self.owner, ADD), (self.admin, REPLACE)):
            with self.subTest(actor=actor.username):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        column = self.pin(users=[self.launcher, self.colleague], mode=mode, actor=actor)
                self.assertEqual(
                    [event.data['change'] for event in board_events(publisher)], ['structure_changed'],
                )
                column.refresh_from_db()
                self.assertEqual(column.pinned_mode, mode)
                self.assertEqual(
                    set(column.pinned_assignees.values_list('pk', flat=True)),
                    {self.launcher.pk, self.colleague.pk},
                )

    def test_the_same_pins_change_nothing_and_an_empty_list_unpins(self):
        self.pin()
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.pin()
        self.assertEqual(board_events(publisher), [])
        self.pin(users=[])
        self.assertFalse(BoardColumnPin.objects.exists())

    def test_refusals(self):
        with self.assertRaisesMessage(BoardError, 'владелец доски или администратор'):
            self.pin(actor=self.member)
        with self.assertRaisesMessage(BoardError, 'только активных участников'):
            self.pin(users=[self.outsider])
        with self.assertRaisesMessage(BoardError, 'завершающую колонку'):
            set_column_pins(done_column_of(self.board), actor=self.owner, user_ids=[self.launcher.pk], mode=ADD)
        with self.assertRaisesMessage(BoardError, 'Неизвестный режим'):
            set_column_pins(self.column('TODO'), actor=self.owner, user_ids=[], mode='SOMETIMES')
        self.assertFalse(BoardColumnPin.objects.exists())
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            self.pin()
        self.assertFalse(BoardColumnPin.objects.exists())

    def test_cards_already_in_the_column_keep_their_people(self):
        card = self.card('Уже здесь', stage='IN_PROGRESS', assignees=[self.member])
        self.pin(mode=REPLACE)
        self.assertEqual(assignees(card), ['member_one'])
        self.assertEqual(self.pin_entries(card), [])

    def test_removing_a_member_drops_their_pins_everywhere(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        self.pin()
        set_column_pins(column_of(self.board, 'TODO', tab), actor=self.owner, user_ids=[self.launcher.pk], mode=ADD)
        self.pin('TODO', users=[self.colleague])
        remove_board_member(self.board, self.launcher, actor=self.owner)
        self.assertEqual(
            list(BoardColumnPin.objects.values_list('user__username', flat=True)), ['member_two'],
        )


class PinsViewTests(PinsFixture, TestCase):
    def url(self, column=None):
        column = column or self.column('IN_PROGRESS')
        return reverse('boards:column_pins', args=[self.board.pk, column.sub_board_id, column.pk])

    def test_route_asks_the_right_before_the_method(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.url()).status_code, 403)
        self.assertEqual(
            self.client.post(self.url(), {'users': [self.launcher.pk], 'mode': ADD}).status_code, 403,
        )
        self.client.force_login(self.owner)
        self.assertRedirects(self.client.get(self.url()), board_url(self.board), fetch_redirect_response=False)
        self.assertFalse(BoardColumnPin.objects.exists())

    def test_owner_pins_by_form_and_the_header_shows_them(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            self.url(), {'users': [self.launcher.pk, self.colleague.pk], 'mode': REPLACE},
        )
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        column = self.column('IN_PROGRESS')
        self.assertEqual(column.pinned_mode, REPLACE)
        self.assertEqual(column.pinned_assignees.count(), 2)
        self.client.force_login(self.member)
        html = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn('class="board-column__pins"', html)
        self.assertIn('Закреплённые исполнители (заменяют)', html)
        # Only the manager has the form.
        self.assertNotIn('boards/%d/%d/columns/%d/pins/' % (self.board.pk, self.main.pk, column.pk), html)
        self.client.force_login(self.owner)
        html = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn(self.url(column), html)
        self.assertIn('value="REPLACE" checked', html)

    def test_a_refusal_is_a_message(self):
        self.client.force_login(self.owner)
        response = self.client.post(self.url(), {'users': [self.outsider.pk], 'mode': ADD}, follow=True)
        self.assertContains(response, 'только активных участников доски')
        response = self.client.post(self.url(), {'users': ['abc'], 'mode': ADD}, follow=True)
        self.assertContains(response, 'Неверный выбор сотрудников')

    def test_more_than_three_pinned_show_plus_n(self):
        extra = [make_user(f'extra_{index}') for index in range(3)]
        add_board_members(self.board, [user.pk for user in extra], actor=self.owner)
        self.pin(users=[self.launcher, self.colleague, *extra])
        self.client.force_login(self.member)
        html = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn('board-avatar--more" aria-hidden="true">+2</span>', html)


class PinsOnCreateTests(PinsFixture, TestCase):
    def test_add_puts_the_pinned_beside_the_chosen(self):
        self.pin('TODO')
        card = self.card('Новая', assignees=[self.colleague], actor=self.member)
        self.assertEqual(assignees(card), ['launcher', 'member_two'])
        self.assertEqual(told(self.launcher), 1)
        self.assertEqual(told(self.colleague), 1)
        kinds = list(card.events.order_by('pk').values_list('kind', flat=True))
        self.assertEqual(kinds, ['CREATED', 'EDITED'])
        entry = self.pin_entries(card)[0]
        self.assertEqual(entry.details['fields'], ['assignees'])
        self.assertEqual(entry.details['by_column'], 'Сделать')
        self.assertEqual(describe_card_event(entry), 'Исполнители по колонке «Сделать»')

    def test_replace_puts_the_pinned_in_place_of_the_chosen(self):
        self.pin('TODO', mode=REPLACE)
        card = self.card('Новая', assignees=[self.colleague], actor=self.member)
        self.assertEqual(assignees(card), ['launcher'])
        # The person replaced before the card existed is never told about it.
        self.assertEqual(told(self.colleague), 0)
        self.assertEqual(told(self.launcher), 1)

    def test_the_chosen_already_pinned_writes_no_entry(self):
        self.pin('TODO')
        card = self.card('Новая', assignees=[self.launcher], actor=self.member)
        self.assertEqual(assignees(card), ['launcher'])
        self.assertEqual(self.pin_entries(card), [])

    def test_an_inactive_pinned_member_is_skipped_and_an_empty_replace_changes_nothing(self):
        self.pin('TODO', mode=REPLACE)
        UserProfile.objects.filter(user=self.launcher).update(is_active=False)
        card = self.card('Новая', assignees=[self.colleague], actor=self.member)
        self.assertEqual(assignees(card), ['member_two'])
        self.assertEqual(self.pin_entries(card), [])
        self.assertEqual(told(self.launcher), 0)

    def test_the_creator_pinned_is_not_told(self):
        self.pin('TODO', users=[self.member])
        self.card('Новая', assignees=[self.colleague], actor=self.member)
        self.assertEqual(told(self.member), 0)


class PinsOnMoveTests(PinsFixture, TestCase):
    def setUp(self):
        self.card_obj = self.card('Заказ', assignees=[self.member], actor=self.owner)
        self.version = self.card_obj.version

    def test_add_on_move_tells_only_the_added_and_records_it(self):
        self.pin()
        before = told(self.member)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                move_card(self.card_obj, actor=self.colleague, column=self.column('IN_PROGRESS'))
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['card_moved'])
        self.assertEqual(assignees(self.card_obj), ['launcher', 'member_one'])
        self.assertEqual(told(self.launcher), 1)
        self.assertEqual(told(self.member), before)
        kinds = list(self.card_obj.events.order_by('pk').values_list('kind', flat=True))
        self.assertEqual(kinds, ['CREATED', 'MOVED', 'EDITED'])
        self.assertEqual(self.pin_entries(self.card_obj)[0].details['by_column'], 'В работе')
        self.card_obj.refresh_from_db()
        self.assertEqual(self.card_obj.version, self.version + 1)

    def test_replace_on_move(self):
        self.pin(mode=REPLACE)
        move_card(self.card_obj, actor=self.colleague, column=self.column('IN_PROGRESS'))
        self.assertEqual(assignees(self.card_obj), ['launcher'])

    def test_a_move_that_changes_no_one_writes_nothing_about_them(self):
        self.pin(users=[self.member])
        move_card(self.card_obj, actor=self.colleague, column=self.column('IN_PROGRESS'))
        self.assertEqual(assignees(self.card_obj), ['member_one'])
        self.assertEqual(self.pin_entries(self.card_obj), [])
        self.card_obj.refresh_from_db()
        self.assertEqual(self.card_obj.version, self.version)

    def test_a_reorder_within_the_column_applies_nothing(self):
        column = self.pin('TODO')
        self.card('Вторая', assignees=[self.member], actor=self.owner)
        launcher_before = told(self.launcher)
        move_card(self.card_obj, actor=self.member, column=column)
        self.assertEqual(assignees(self.card_obj), ['member_one'])
        self.assertEqual(told(self.launcher), launcher_before)

    def test_by_drag(self):
        self.pin()
        self.client.force_login(self.colleague)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card_obj.pk]),
            {'column_id': self.column('IN_PROGRESS').pk}, **FETCH,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(assignees(self.card_obj), ['launcher', 'member_one'])
        self.assertEqual(len(self.pin_entries(self.card_obj)), 1)

    def test_by_move_to(self):
        self.pin()
        self.client.force_login(self.colleague)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card_obj.pk]),
            {'column_id': self.column('IN_PROGRESS').pk},
        )
        self.assertRedirects(
            response, board_url(self.board) + f'?card={self.card_obj.pk}', fetch_redirect_response=False,
        )
        self.assertEqual(assignees(self.card_obj), ['launcher', 'member_one'])

    def test_by_moving_to_another_sub_board(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        target = column_of(self.board, 'REVIEW', tab)
        set_column_pins(target, actor=self.owner, user_ids=[self.launcher.pk], mode=REPLACE)
        move_card(self.card_obj, actor=self.colleague, column=target)
        self.assertEqual(assignees(self.card_obj), ['launcher'])
        entry = self.pin_entries(self.card_obj)[0]
        self.assertEqual(describe_card_event(entry), 'Исполнители по колонке «На проверке»')


class CrossSubBoardMoveTests(PinsFixture, TestCase):
    def setUp(self):
        self.tab = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        self.card_obj = self.card('Заказ', assignees=[self.member], actor=self.owner)
        self.staying = self.card('Остаётся', assignees=[self.member], actor=self.owner)
        new_card(self.board, self.owner, 'Там уже', assignees=[self.member],
                 column=column_of(self.board, 'IN_PROGRESS', self.tab))
        self.number = self.card_obj.number

    def target(self, stage='IN_PROGRESS'):
        return column_of(self.board, stage, self.tab)

    def test_moves_to_the_end_of_the_column_keeping_the_number(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                move_card(self.card_obj, actor=self.member, column=self.target())
        self.assertEqual(
            [(event.data['change'], event.data['card_id']) for event in board_events(publisher)],
            [('card_moved', self.card_obj.pk)],
        )
        self.card_obj.refresh_from_db()
        self.assertEqual(self.card_obj.sub_board, self.tab)
        self.assertEqual(self.card_obj.column, self.target())
        self.assertEqual(self.card_obj.number, self.number)
        order = list(
            self.tab.cards.filter(column=self.target()).order_by('position').values_list('title', flat=True)
        )
        self.assertEqual(order, ['Там уже', 'Заказ'])

    def test_journal_names_both_sub_boards(self):
        move_card(self.card_obj, actor=self.member, column=self.target())
        event = self.card_obj.events.get(kind=BoardCardEvent.Kind.MOVED)
        self.assertEqual(event.details['from_sub_board'], 'Основная')
        self.assertEqual(event.details['to_sub_board'], 'Цех ПиР')
        self.assertEqual(event.details['to_sub_board_id'], self.tab.pk)
        self.assertEqual(describe_card_event(event), 'Перенос: «Основная / Сделать» → «Цех ПиР / В работе»')
        # Within one sub-board, the entry names columns only, as before.
        move_card(self.staying, actor=self.member, column=self.column('IN_PROGRESS'))
        event = self.staying.events.get(kind=BoardCardEvent.Kind.MOVED)
        self.assertNotIn('to_sub_board', event.details)
        self.assertEqual(describe_card_event(event), 'Перенос: «Сделать» → «В работе»')

    def test_refusals(self):
        with self.assertRaisesMessage(BoardError, 'завершите задачу'):
            move_card(self.card_obj, actor=self.member, column=done_column_of(self.board, self.tab))
        with self.assertRaisesMessage(BoardError, 'недоступна'):
            move_card(self.card_obj, actor=self.outsider, column=self.target())
        self.card_obj.refresh_from_db()
        self.assertEqual(self.card_obj.sub_board, self.main)

    def test_move_to_offers_every_sub_board_grouped_current_first(self):
        self.client.force_login(self.member)
        card_html = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()['facts_html']
        self.assertIn('aria-label="Переместить в колонку"', card_html)
        main_group = card_html.index('<optgroup label="Основная">')
        tab_group = card_html.index('<optgroup label="Цех ПиР">')
        self.assertLess(main_group, tab_group)
        self.assertIn(f'<option value="{self.target().pk}">В работе</option>', card_html)
        self.assertNotIn(f'value="{done_column_of(self.board, self.tab).pk}"', card_html)

    def test_the_form_goes_to_the_cards_new_sub_board(self):
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card_obj.pk]),
            {'column_id': self.target().pk},
        )
        self.assertRedirects(
            response, board_url(self.board, self.tab) + f'?card={self.card_obj.pk}',
            fetch_redirect_response=False,
        )

    def test_both_fragments_follow_and_the_open_panel_says_where(self):
        self.client.force_login(self.colleague)
        old = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()
        self.assertIn(f'data-card-id="{self.card_obj.pk}"', old['columns_html'])
        self.assertNotIn('board-drawer__moved', old['panel_html'])
        move_card(self.card_obj, actor=self.member, column=self.target())

        old_after = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()
        self.assertNotIn(f'data-card-id="{self.card_obj.pk}"', old_after['columns_html'])
        self.assertNotEqual(old['panel_revision'], old_after['panel_revision'])
        self.assertIn('Карточка перенесена на поддоску «', old_after['panel_html'])
        there = board_url(self.board, self.tab) + f'?card={self.card_obj.pk}&amp;tab=description'
        self.assertIn(f'href="{there}"', old_after['panel_html'])
        # It stays where it was asked for: the panel of the old sub-board.
        self.assertEqual(old_after['card_id'], self.card_obj.pk)
        self.assertEqual(old_after['panel'], 'view')

        new = self.client.get(fragment_url(self.board, self.tab), {'card': self.card_obj.pk}).json()
        self.assertIn(f'data-card-id="{self.card_obj.pk}"', new['columns_html'])
        self.assertNotIn('board-drawer__moved', new['panel_html'])

    def test_the_page_of_the_old_sub_board_matches_its_fragment(self):
        from .helpers import assert_page_matches_fragment

        move_card(self.card_obj, actor=self.member, column=self.target())
        self.client.force_login(self.colleague)
        page = self.client.get(board_url(self.board), {'card': self.card_obj.pk})
        fragment = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()
        assert_page_matches_fragment(self, page.content.decode(), fragment)
