"""Stage 25: a column's «Правила при входе», the board's actions «Передать
дальше», «📌 Закрепить» and «Списком».

The entry rules act wherever the pins act — a card created in the column or
moved into it by a drag, «Переместить в…», from another sub-board or by an
action — and never on a reorder within the column; what they did is one
journal entry. An action is the move, the people, the field values and the
message of «Чат», in one transaction and one `board.updated(card_moved)`.
"""

from contextlib import contextmanager
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from notifications.models import Notification
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task

from ..models import (
    MAX_ACTIONS,
    MAX_CHECKLIST_ITEMS,
    MAX_TEMPLATE_ITEMS,
    BoardAction,
    BoardCard,
    BoardCardChecklistItem,
    BoardCardComment,
    BoardCardEvent,
    BoardCardFieldValue,
    BoardCardSubscription,
    BoardColumn,
    BoardColumnChecklistTemplate,
    BoardColumnFollower,
    BoardField,
)
from ..selectors import build_board_state, describe_card_event
from ..services import (
    MAX_LIST_CARDS,
    BoardError,
    FieldValueError,
    action_message,
    add_board_members,
    add_checklist_item,
    add_template_item,
    archive_action,
    archive_field,
    create_action,
    create_board,
    create_cards_from_list,
    create_field,
    create_sub_board,
    create_subtask,
    delete_column,
    delete_field,
    delete_option,
    delete_sub_board,
    delete_template_item,
    move_action,
    move_card,
    move_template_item,
    remove_board_member,
    run_board_action,
    set_card_pinned,
    set_column_field_rules,
    set_column_followers,
    set_column_pins,
    update_action,
    update_card,
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    column_of,
    due,
    fragment_url,
    fresh_code,
    make_user,
    new_card,
)


FETCH = {'HTTP_X_REQUESTED_WITH': 'fetch'}


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def people(card):
    return sorted(task_of(card).assignees.values_list('user__username', flat=True))


def checklist(card):
    return list(BoardCardChecklistItem.objects.filter(card=card).order_by('position').values_list('text', flat=True))


def value_of(card, field):
    row = BoardCardFieldValue.objects.filter(card=card, field=field).first()
    if row is None:
        return None
    return row.option_id if field.kind == BoardField.Kind.SELECT else (
        row.value_text if row.value_text is not None else row.value_number
    )


def rule_entries(card):
    return list(
        BoardCardEvent.objects.filter(card=card, kind=BoardCardEvent.Kind.EDITED, details__has_key='by_column')
        .order_by('pk')
    )


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


@contextmanager
def published(test):
    """What the block publishes once its transaction commits."""
    with capture_realtime_events() as publisher, test.captureOnCommitCallbacks(execute=True):
        yield publisher


class RulesFixture(BoardFixtureMixin):
    """The fixture board, a text field «Цех», a list field «Приоритет», a
    launcher member and «На проверке» as «Запуск в работу»."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.launcher = make_user('launcher', UserProfile.Role.PDO)
        add_board_members(cls.board, [cls.launcher.pk], actor=cls.owner)
        cls.shop = create_field(cls.board, actor=cls.owner, name='Цех', kind=BoardField.Kind.TEXT)
        cls.priority = create_field(
            cls.board, actor=cls.owner, name='Приоритет', kind=BoardField.Kind.SELECT,
            options=[('Высокий', 'red'), ('Обычный', 'gray')],
        )
        cls.high, cls.normal = list(cls.priority.options.order_by('position'))

    def launch(self):
        return self.column('REVIEW')

    def template(self, *lines, column=None):
        column = column or self.launch()
        for line in lines:
            add_template_item(column, actor=self.owner, text=line)
        return column


class TemplateOnEntryTests(RulesFixture, TestCase):
    def test_created_in_the_column_the_card_gets_the_lines_at_the_end(self):
        column = self.template('Проверить КД', 'Заказать материал', 'Запустить в цех')
        card = self.card(stage='REVIEW')
        self.assertEqual(checklist(card), ['Проверить КД', 'Заказать материал', 'Запустить в цех'])
        entry, = rule_entries(card)
        self.assertEqual(entry.details['checklist_added'], 3)
        self.assertEqual(entry.details['by_column'], column.name)
        self.assertEqual(entry.details['fields'], [])
        self.assertEqual(describe_card_event(entry), f'Правила колонки «{column.name}»: чек-лист +3')

    def test_every_move_path_applies_them(self):
        self.template('Проверить КД')
        # A drag (the JSON answer of `card_move`).
        dragged = self.card('Тянем')
        self.client.force_login(self.member)
        url = reverse('boards:card_move', args=[self.board.pk, dragged.pk])
        self.assertEqual(self.client.post(url, {'column_id': self.launch().pk}, **FETCH).status_code, 200)
        self.assertEqual(checklist(dragged), ['Проверить КД'])
        # «Переместить в…», the ordinary form.
        moved = self.card('Переносим')
        self.client.post(
            reverse('boards:card_move', args=[self.board.pk, moved.pk]), {'column_id': self.launch().pk},
        )
        self.assertEqual(checklist(moved), ['Проверить КД'])
        # From another sub-board.
        tab = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        far = new_card(self.board, self.member, 'Издалека', assignees=[self.member], sub_board=tab,
                       column=column_of(self.board, 'TODO', tab))
        move_card(far, actor=self.member, column=self.launch())
        self.assertEqual(checklist(far), ['Проверить КД'])

    def test_a_reorder_within_the_column_applies_nothing(self):
        first = self.card('Первая', stage='REVIEW')
        second = self.card('Вторая', stage='REVIEW')
        self.template('Проверить КД')
        move_card(second, actor=self.member, column=self.launch(), before_card_id=first.pk)
        self.assertEqual(checklist(second), [])
        self.assertEqual(rule_entries(second), [])

    def test_a_line_already_held_is_not_repeated_whatever_the_case(self):
        self.template('Проверить КД', 'Запустить в цех')
        card = self.card()
        add_checklist_item(card, actor=self.member, text='проверить кд')
        move_card(card, actor=self.member, column=self.launch())
        self.assertEqual(checklist(card), ['проверить кд', 'Запустить в цех'])
        self.assertEqual(rule_entries(card)[0].details['checklist_added'], 1)

    def test_the_checklist_limit_holds_and_the_rest_is_counted(self):
        self.template('Первый', 'Второй', 'Третий')
        card = self.card()
        for index in range(MAX_CHECKLIST_ITEMS - 1):
            add_checklist_item(card, actor=self.member, text=f'Пункт {index}')
        move_card(card, actor=self.member, column=self.launch())
        items = checklist(card)
        self.assertEqual(len(items), MAX_CHECKLIST_ITEMS)
        self.assertEqual(items[-1], 'Первый')
        entry, = rule_entries(card)
        self.assertEqual((entry.details['checklist_added'], entry.details['checklist_skipped']), (1, 2))
        self.assertIn('не вошло в чек-лист: 2', describe_card_event(entry))


class FieldRulesOnEntryTests(RulesFixture, TestCase):
    def set_rules(self, **values):
        set_column_field_rules(self.launch(), actor=self.owner, values=values)

    def test_sets_an_empty_value_and_leaves_a_set_one(self):
        set_column_field_rules(self.launch(), actor=self.owner, values={
            self.shop.pk: ('Цех ПиР', False), self.priority.pk: (str(self.high.pk), False),
        })
        empty = self.card('Пустая')
        held = self.card('С приоритетом', field_values={self.priority.pk: str(self.normal.pk)})
        for card in (empty, held):
            move_card(card, actor=self.member, column=self.launch())
        self.assertEqual((value_of(empty, self.shop), value_of(empty, self.priority)), ('Цех ПиР', self.high.pk))
        self.assertEqual(value_of(held, self.priority), self.normal.pk, 'a rule sets, it does not reset')
        entry, = rule_entries(empty)
        self.assertEqual(entry.details['fields'], ['custom'])
        self.assertEqual(entry.details['custom_fields'], ['Цех', 'Приоритет'])
        self.assertNotIn('Цех ПиР', str(entry.details), 'no value in the journal')

    def test_overwrite_replaces_a_different_value(self):
        set_column_field_rules(self.launch(), actor=self.owner, values={self.priority.pk: (str(self.high.pk), True)})
        card = self.card(field_values={self.priority.pk: str(self.normal.pk)})
        version = BoardCard.objects.get(pk=card.pk).version
        move_card(card, actor=self.member, column=self.launch())
        self.assertEqual(value_of(card, self.priority), self.high.pk)
        self.assertEqual(BoardCard.objects.get(pk=card.pk).version, version + 1, 'an edit form drawn before is stale')

    def test_an_archived_field_is_skipped(self):
        set_column_field_rules(self.launch(), actor=self.owner, values={self.shop.pk: ('Цех ПиР', False)})
        archive_field(self.shop, actor=self.owner)
        card = self.card()
        move_card(card, actor=self.member, column=self.launch())
        self.assertIsNone(value_of(card, self.shop))
        self.assertEqual(rule_entries(card), [])

    def test_a_value_is_parsed_like_a_cards_and_refused_beside_its_field(self):
        number = create_field(self.board, actor=self.owner, name='Кол-во', kind=BoardField.Kind.NUMBER)
        with self.assertRaises(FieldValueError) as caught:
            set_column_field_rules(self.launch(), actor=self.owner, values={number.pk: ('двенадцать', False)})
        self.assertEqual(caught.exception.field_id, number.pk)
        self.assertTrue(set_column_field_rules(self.launch(), actor=self.owner, values={number.pk: ('1 250,5', False)}))
        self.assertEqual(self.launch().field_rules.get().value, '1250.5')
        # The same again: nothing changes, nothing is published.
        with published(self) as publisher:
            self.assertFalse(
                set_column_field_rules(self.launch(), actor=self.owner, values={number.pk: ('1250.5', False)})
            )
        self.assertEqual(board_events(publisher), [])
        # Empty removes it.
        self.assertTrue(set_column_field_rules(self.launch(), actor=self.owner, values={number.pk: ('', False)}))
        self.assertFalse(self.launch().field_rules.exists())

    def test_a_field_or_option_a_rule_sets_is_not_deleted_and_keeps_its_kind(self):
        set_column_field_rules(self.launch(), actor=self.owner, values={
            self.shop.pk: ('ПиР', False), self.priority.pk: (str(self.high.pk), False),
        })
        with self.assertRaisesMessage(BoardError, 'правила колонок'):
            delete_field(self.shop, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'правила колонок'):
            delete_option(self.high, actor=self.owner)


class FollowersAndJournalTests(RulesFixture, TestCase):
    def test_followers_are_readers_only_and_follow_the_card(self):
        with self.assertRaisesMessage(BoardError, 'только активных читателей'):
            set_column_followers(self.launch(), actor=self.owner, user_ids=[self.outsider.pk + 1000])
        set_column_followers(self.launch(), actor=self.owner, user_ids=[self.launcher.pk, self.colleague.pk])
        card = self.card()
        BoardCardSubscription.objects.create(card=card, user=self.colleague)
        move_card(card, actor=self.member, column=self.launch())
        self.assertEqual(
            set(BoardCardSubscription.objects.filter(card=card).values_list('user_id', flat=True)),
            {self.launcher.pk, self.colleague.pk},
        )
        self.assertEqual(rule_entries(card)[0].details['followers_added'], 1)
        # Taken off the board: no longer a follower of the column.
        remove_board_member(self.board, self.launcher, actor=self.owner)
        self.assertFalse(BoardColumnFollower.objects.filter(user=self.launcher).exists())

    def test_all_four_rules_are_one_journal_entry(self):
        column = self.launch()
        set_column_pins(column, actor=self.owner, user_ids=[self.launcher.pk], mode=BoardColumn.PinnedMode.ADD)
        self.template('Проверить КД')
        set_column_field_rules(column, actor=self.owner, values={self.shop.pk: ('ПиР', False)})
        set_column_followers(column, actor=self.owner, user_ids=[self.colleague.pk])
        card = self.card()
        with published(self) as publisher:
            move_card(card, actor=self.member, column=column)
        entry, = rule_entries(card)
        self.assertEqual(entry.details['fields'], ['assignees', 'custom'])
        self.assertEqual(
            (entry.details['checklist_added'], entry.details['followers_added']), (1, 1),
        )
        self.assertEqual(
            describe_card_event(entry),
            f'Правила колонки «{column.name}»: исполнители, Цех, чек-лист +1, подписчики +1',
        )
        self.assertEqual(people(card), ['launcher', 'member_one'])
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['card_moved'])

    def test_subtasks_stand_in_no_column_and_get_nothing(self):
        self.template('Проверить КД')
        card = self.card(stage='REVIEW')
        subtask = create_subtask(card, actor=self.member, title='Позиция')
        self.assertEqual(checklist(subtask), [])


class RulesSettingsTests(RulesFixture, TestCase):
    def test_template_limit_duplicates_order_and_delete(self):
        column = self.launch()
        add_template_item(column, actor=self.owner, text='Один')
        with self.assertRaisesMessage(BoardError, 'уже есть'):
            add_template_item(column, actor=self.owner, text='один')
        second = add_template_item(column, actor=self.owner, text='Два')
        move_template_item(second, actor=self.owner, direction='left')
        self.assertEqual(
            list(BoardColumnChecklistTemplate.objects.filter(column=column).order_by('position').values_list('text', flat=True)),
            ['Два', 'Один'],
        )
        delete_template_item(second, actor=self.owner)
        for index in range(MAX_TEMPLATE_ITEMS - 1):
            add_template_item(column, actor=self.owner, text=f'Шаг {index}')
        with self.assertRaisesMessage(BoardError, f'не больше {MAX_TEMPLATE_ITEMS}'):
            add_template_item(column, actor=self.owner, text='Лишний')

    def test_manager_only_never_the_closing_column_nor_an_archived_board(self):
        with self.assertRaises(BoardError):
            add_template_item(self.launch(), actor=self.member, text='Шаг')
        done = BoardColumn.objects.get(sub_board=self.main, is_done=True)
        with self.assertRaisesMessage(BoardError, 'завершающую колонку'):
            add_template_item(done, actor=self.owner, text='Шаг')
        with published(self) as publisher:
            add_template_item(self.launch(), actor=self.admin, text='Шаг')
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['structure_changed'])

    def test_the_header_shows_a_gear_once_there_are_rules(self):
        self.client.force_login(self.member)
        rules_url = reverse('boards:column_rules', args=[self.board.pk, self.launch().pk])
        self.assertNotIn(f'href="{rules_url}"', self.client.get(fragment_url(self.board)).json()['columns_html'])
        self.template('Проверить КД')
        self.assertIn('class="board-column__rules"', self.client.get(fragment_url(self.board)).json()['columns_html'])


class RulesRoutesTests(RulesFixture, TestCase):
    def url(self, name, *extra):
        return reverse(name, args=[self.board.pk, self.launch().pk, *extra])

    def test_the_page_reads_for_readers_and_edits_for_the_manager(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url('boards:column_rules'))
        self.assertContains(response, 'Правила при входе')
        self.assertNotContains(response, self.url('boards:column_template_add'))
        self.client.force_login(self.owner)
        response = self.client.get(self.url('boards:column_rules'))
        self.assertContains(response, self.url('boards:column_template_add'))
        self.assertContains(response, 'name="back" value="rules"')
        done = BoardColumn.objects.get(sub_board=self.main, is_done=True)
        self.assertEqual(
            self.client.get(reverse('boards:column_rules', args=[self.board.pk, done.pk])).status_code, 404,
        )

    def test_writes_ask_the_right_before_the_method_and_a_get_changes_nothing(self):
        routes = [
            self.url('boards:column_template_add'),
            self.url('boards:column_rules_fields'),
            self.url('boards:column_rules_followers'),
        ]
        self.client.force_login(self.member)
        for route in routes:
            with self.subTest(route=route):
                self.assertEqual(self.client.get(route).status_code, 403)
                self.assertEqual(self.client.post(route, {'text': 'Шаг'}).status_code, 403)
        self.client.force_login(self.owner)
        for route in routes:
            with self.subTest(route=route):
                self.assertRedirects(self.client.get(route), self.url('boards:column_rules'), fetch_redirect_response=False)
        self.assertFalse(BoardColumnChecklistTemplate.objects.exists())

    def test_forms_write_and_a_refused_value_comes_back_beside_its_field(self):
        self.client.force_login(self.owner)
        self.client.post(self.url('boards:column_template_add'), {'text': 'Проверить КД'})
        self.assertEqual(self.launch().checklist_template.get().text, 'Проверить КД')
        date_field = create_field(self.board, actor=self.owner, name='Срок изг.', kind=BoardField.Kind.DATE)
        response = self.client.post(self.url('boards:column_rules_fields'), {
            f'rule_{self.shop.pk}': 'ПиР', f'rule_{date_field.pk}': '31-31-2026',
        })
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'дата в формате ГГГГ-ММ-ДД', status_code=400)
        self.assertContains(response, 'value="ПиР"', status_code=400)
        self.assertFalse(self.launch().field_rules.exists(), 'nothing of a refused form is stored')
        response = self.client.post(self.url('boards:column_rules_fields'), {
            f'rule_{self.shop.pk}': 'ПиР', f'overwrite_{self.shop.pk}': 'on',
        })
        self.assertRedirects(response, self.url('boards:column_rules') + '#fields', fetch_redirect_response=False)
        rule = self.launch().field_rules.get()
        self.assertEqual((rule.value, rule.overwrite), ('ПиР', True))
        self.client.post(self.url('boards:column_rules_followers'), {'users': [self.colleague.pk]})
        self.assertEqual(list(self.launch().entry_followers.values_list('user_id', flat=True)), [self.colleague.pk])
        # The pins posted from the page come back to it.
        pins = reverse('boards:column_pins', args=[self.board.pk, self.main.pk, self.launch().pk])
        response = self.client.post(pins, {'users': [self.launcher.pk], 'mode': 'ADD', 'back': 'rules'})
        self.assertRedirects(response, self.url('boards:column_rules') + '#pins', fetch_redirect_response=False)


class ActionsFixture(RulesFixture):
    def action(self, name='Передать в ПДО', *, column=None, mode=BoardAction.AssigneeMode.REPLACE,
               assignees=None, **extra):
        return create_action(
            self.board, actor=self.owner, name=name, target_column=column or self.launch(),
            assignee_mode=mode,
            assignee_ids=[user.pk for user in (assignees if assignees is not None else [self.launcher])],
            **extra,
        )


class ActionSettingsTests(ActionsFixture, TestCase):
    def test_limit_names_order_and_archive(self):
        first = self.action('Передать в ПДО')
        with self.assertRaisesMessage(BoardError, 'уже есть'):
            self.action('передать в пдо')
        others = [self.action(f'Действие {index}') for index in range(MAX_ACTIONS - 1)]
        with self.assertRaisesMessage(BoardError, f'уже {MAX_ACTIONS} действий'):
            self.action('Лишнее')
        archive_action(others[0], actor=self.owner)
        self.action('Лишнее')
        with self.assertRaisesMessage(BoardError, 'вернуть ещё одно нельзя'):
            archive_action(others[0], actor=self.owner, archived=False)
        move_action(first, actor=self.owner, direction='right')
        self.assertEqual(BoardAction.objects.get(pk=first.pk).position, 2)

    def test_add_or_replace_needs_people_and_the_column_is_of_this_board(self):
        with self.assertRaisesMessage(BoardError, 'хотя бы одного исполнителя'):
            self.action(assignees=[])
        other = create_board(code=fresh_code(), name='Чужая', owner=self.owner, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Колонка не найдена на этой доске'):
            self.action(column=column_of(other, 'TODO'))
        done = BoardColumn.objects.get(sub_board=self.main, is_done=True)
        with self.assertRaisesMessage(BoardError, 'завершите задачу'):
            self.action(column=done)

    def test_the_same_settings_change_nothing(self):
        action = self.action(message_template='Передано: {код}')
        with published(self) as publisher:
            update_action(
                action, actor=self.owner, name='Передать в ПДО', target_column=self.launch(),
                assignee_mode=BoardAction.AssigneeMode.REPLACE, assignee_ids=[self.launcher.pk],
                message_template='Передано: {код}',
            )
        self.assertEqual(board_events(publisher), [])
        with published(self) as publisher:
            update_action(
                action, actor=self.owner, name='В ПДО', target_column=self.launch(),
                assignee_mode=BoardAction.AssigneeMode.KEEP,
            )
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['structure_changed'])
        self.assertFalse(BoardAction.objects.get(pk=action.pk).assignee_rows.exists(), 'KEEP keeps nobody')

    def test_a_column_an_action_leads_to_is_not_deleted(self):
        self.action(column=self.column('IN_PROGRESS'))
        with self.assertRaisesMessage(BoardError, 'ведут действия доски: «Передать в ПДО»'):
            delete_column(self.column('IN_PROGRESS'), actor=self.owner)
        tab = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        self.action('На ПиР', column=column_of(self.board, 'TODO', tab))
        with self.assertRaisesMessage(BoardError, '«На ПиР»'):
            delete_sub_board(tab, actor=self.owner)

    def test_a_member_taken_off_leaves_the_actions(self):
        action = self.action()
        remove_board_member(self.board, self.launcher, actor=self.owner)
        self.assertFalse(action.assignee_rows.exists())


class RunActionTests(ActionsFixture, TestCase):
    def test_all_four_steps_one_event(self):
        action = self.action(
            message_template='{код} передана в «{колонка}»', comment_required=True,
            field_values={self.shop.pk: ('ПДО', True)},
        )
        self.template('Проверить КД')
        card = self.card(field_values={self.shop.pk: 'ОП'})
        with published(self) as publisher:
            run_board_action(card, action, actor=self.member, comment='Срочно')
        card.refresh_from_db()
        self.assertEqual(card.column_id, self.launch().pk)
        self.assertEqual(people(card), ['launcher'])
        self.assertEqual(value_of(card, self.shop), 'ПДО')
        self.assertEqual(checklist(card), ['Проверить КД'], 'the column rules ran too')
        message = BoardCardComment.objects.get(card=card)
        self.assertEqual(message.author, self.member)
        self.assertEqual(message.text, f'{card.code} передана в «{self.launch().name}»\n\nСрочно')
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['card_moved'])
        moved = BoardCardEvent.objects.get(card=card, kind=BoardCardEvent.Kind.MOVED)
        self.assertEqual((moved.details['action_id'], moved.details['action']), (action.pk, 'Передать в ПДО'))
        self.assertIn('действие «Передать в ПДО»', describe_card_event(moved))
        by_action = BoardCardEvent.objects.get(card=card, kind=BoardCardEvent.Kind.EDITED, details__has_key='by_action')
        self.assertEqual(describe_card_event(by_action), 'По действию «Передать в ПДО»: исполнители, Цех')
        # The new person is told once; the presser is not.
        self.assertEqual(Notification.objects.filter(
            event_type=Notification.EventType.BOARD_TASK_ASSIGNED, recipient=self.launcher,
        ).count(), 1)
        self.assertTrue(Notification.objects.filter(
            event_type=Notification.EventType.BOARD_CARD_COMMENT, recipient=self.launcher,
        ).exists())
        self.assertFalse(Notification.objects.filter(recipient=self.member, actor=self.member).exists())

    def test_a_required_comment_and_a_template_without_one(self):
        required = self.action(comment_required=True)
        card = self.card()
        with self.assertRaisesMessage(BoardError, 'нужен комментарий'):
            run_board_action(card, required, actor=self.member, comment='   ')
        self.assertEqual(BoardCard.objects.get(pk=card.pk).column_id, self.column('TODO').pk)
        self.assertEqual(action_message(required, card, self.launch(), ''), '')

    def test_add_and_keep(self):
        add = self.action('Добавить', mode=BoardAction.AssigneeMode.ADD)
        keep = self.action('Оставить', column=self.column('IN_PROGRESS'), mode=BoardAction.AssigneeMode.KEEP)
        card = self.card()
        run_board_action(card, keep, actor=self.member)
        self.assertEqual(people(card), ['member_one'])
        self.assertFalse(BoardCardComment.objects.filter(card=card).exists(), 'no template, no comment, no message')
        run_board_action(card, add, actor=self.member)
        self.assertEqual(people(card), ['launcher', 'member_one'])

    def test_a_failing_step_rolls_everything_back(self):
        action = self.action(message_template='Передано')
        self.template('Проверить КД')
        card = self.card()
        with mock.patch('notifications.services.notify_board_card_comment', side_effect=RuntimeError('почта')):
            with published(self) as publisher, self.assertRaises(RuntimeError):
                run_board_action(card, action, actor=self.member)
        card.refresh_from_db()
        self.assertEqual(card.column_id, self.column('TODO').pk)
        self.assertEqual(people(card), ['member_one'])
        self.assertEqual(checklist(card), [])
        self.assertFalse(BoardCardComment.objects.filter(card=card).exists())
        self.assertFalse(BoardCardEvent.objects.filter(card=card, kind=BoardCardEvent.Kind.MOVED).exists())
        self.assertEqual(board_events(publisher), [])

    def test_refusals(self):
        action = self.action()
        card = self.card(stage='REVIEW')
        with self.assertRaisesMessage(BoardError, 'уже стоит в колонке'):
            run_board_action(card, action, actor=self.member)
        subtask = create_subtask(self.card(), actor=self.member, title='Позиция')
        with self.assertRaisesMessage(BoardError, 'Подзадачу не переносят'):
            run_board_action(subtask, action, actor=self.member)
        archive_action(action, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'убрано в архив'):
            run_board_action(self.card(), action, actor=self.member)
        live = self.action('Ещё')
        with self.assertRaises(BoardError):
            run_board_action(self.card(), live, actor=self.outsider)


class ActionButtonsTests(ActionsFixture, TestCase):
    def panel(self, card, user=None):
        self.client.force_login(user or self.member)
        return self.client.get(fragment_url(self.board), {'card': card.pk}).json()['panel_html']

    def test_three_buttons_then_more_and_never_the_current_column(self):
        here = self.action('Сюда', column=self.column('TODO'))
        names = ['Первое', 'Второе', 'Третье', 'Четвёртое']
        for name in names:
            self.action(name)
        card = self.card()
        html = self.panel(card)
        self.assertNotIn(reverse('boards:card_action', args=[self.board.pk, card.pk, here.pk]), html)
        for name in names:
            self.assertIn(f'>{name}</button>', html)
        self.assertIn('Ещё ▾', html)
        self.assertLess(html.index('Третье'), html.index('Ещё ▾'))
        self.assertLess(html.index('Ещё ▾'), html.index('Четвёртое'))
        self.assertIn(f'data-confirm-title="Передать {card.code} в «{self.launch().name}»?"', html)

    def test_no_buttons_for_a_closed_card_a_subtask_or_without_the_right(self):
        action = self.action(comment_required=True)
        card = self.card()
        button = reverse('boards:card_action', args=[self.board.pk, card.pk, action.pk])
        html = self.panel(card)
        self.assertIn(button, html)
        self.assertIn('data-confirm-comment="required"', html)
        self.assertNotIn(button, self.panel(card, self.outsider))
        subtask = create_subtask(card, actor=self.member, title='Позиция')
        self.assertNotIn('data-board-actions', self.panel(subtask))

    def test_the_route(self):
        action = self.action(message_template='В работу')
        card = self.card()
        url = reverse('boards:card_action', args=[self.board.pk, card.pk, action.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.member)
        self.assertRedirects(self.client.get(url), board_url(self.board) + f'?card={card.pk}',
                             fetch_redirect_response=False)
        self.assertEqual(BoardCard.objects.get(pk=card.pk).column_id, self.column('TODO').pk)
        response = self.client.post(url)
        self.assertRedirects(response, board_url(self.board) + f'?card={card.pk}', fetch_redirect_response=False)
        self.assertEqual(BoardCard.objects.get(pk=card.pk).column_id, self.launch().pk)
        # Pressed again from a stale tab: the panel says why.
        response = self.client.post(url)
        self.assertContains(response, 'уже стоит в колонке')


class ActionsPageTests(ActionsFixture, TestCase):
    def test_readers_read_the_manager_sets_up(self):
        self.action()
        url = reverse('boards:actions', args=[self.board.pk])
        self.client.force_login(self.member)
        response = self.client.get(url)
        self.assertContains(response, 'Передать в ПДО')
        self.assertNotContains(response, reverse('boards:action_create', args=[self.board.pk]))
        self.assertEqual(self.client.post(reverse('boards:action_create', args=[self.board.pk])).status_code, 403)
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(url + '?new=1'), reverse('boards:action_create', args=[self.board.pk]))

    def test_create_by_form_and_a_refusal_keeps_the_input(self):
        self.client.force_login(self.owner)
        create_url = reverse('boards:action_create', args=[self.board.pk])
        response = self.client.post(create_url, {
            'name': 'Передать в ПДО', 'target_column': self.launch().pk, 'assignee_mode': 'REPLACE',
            'message_template': 'Передано: {код}',
        })
        self.assertContains(response, 'хотя бы одного исполнителя', status_code=400)
        self.assertContains(response, 'Передано: {код}', status_code=400)
        response = self.client.post(create_url, {
            'name': 'Передать в ПДО', 'target_column': self.launch().pk, 'assignee_mode': 'REPLACE',
            'assignees': [self.launcher.pk], 'message_template': 'Передано: {код}',
            f'rule_{self.priority.pk}': str(self.high.pk), 'comment_required': 'on',
        })
        action = BoardAction.objects.get(board=self.board)
        self.assertRedirects(
            response, reverse('boards:actions', args=[self.board.pk]) + f'#action-{action.pk}',
            fetch_redirect_response=False,
        )
        self.assertTrue(action.comment_required)
        self.assertEqual(action.field_rules.get().value, str(self.high.pk))
        self.assertEqual(self.client.get(reverse('boards:action_archive', args=[self.board.pk, action.pk])).status_code, 302)
        self.assertFalse(BoardAction.objects.get(pk=action.pk).is_archived, 'a GET changes nothing')


class PinnedCardTests(RulesFixture, TestCase):
    def tiles(self):
        state = build_board_state(self.board, self.main, self.member)
        column = next(row for row in state['columns'] if row['column'].pk == self.column('TODO').pk)
        return [item['card'].title for item in column['cards']]

    def test_pinned_first_in_their_own_order_and_one_event(self):
        first, second, third = (self.card(title) for title in ('Первая', 'Вторая', 'Третья'))
        with published(self) as publisher:
            set_card_pinned(third, actor=self.member, pinned=True)
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['card_updated'])
        set_card_pinned(second, actor=self.member, pinned=True)
        self.assertEqual(self.tiles(), ['Третья', 'Вторая', 'Первая'])
        with published(self) as publisher:
            set_card_pinned(second, actor=self.member, pinned=True)
        self.assertEqual(board_events(publisher), [], 'the same state changes nothing')
        set_card_pinned(third, actor=self.member, pinned=False)
        self.assertEqual(self.tiles(), ['Вторая', 'Третья', 'Первая'])
        self.assertFalse(first.is_pinned)

    def test_a_drag_keeps_each_group(self):
        first, second, third = (self.card(title) for title in ('Первая', 'Вторая', 'Третья'))
        set_card_pinned(second, actor=self.member, pinned=True)
        set_card_pinned(third, actor=self.member, pinned=True)
        # A pinned card before an unpinned one: the end of the pinned.
        move_card(second, actor=self.member, column=self.column('TODO'), before_card_id=first.pk)
        self.assertEqual(self.tiles(), ['Третья', 'Вторая', 'Первая'])
        # An unpinned card before a pinned one: the start of the others.
        move_card(first, actor=self.member, column=self.column('TODO'), before_card_id=third.pk)
        self.assertEqual(self.tiles(), ['Третья', 'Вторая', 'Первая'])
        move_card(second, actor=self.member, column=self.column('TODO'), before_card_id=third.pk)
        self.assertEqual(self.tiles(), ['Вторая', 'Третья', 'Первая'])

    def test_leaving_the_column_unpins_and_the_route(self):
        card = self.card()
        url = reverse('boards:card_pin', args=[self.board.pk, card.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url, {'pinned': '1'}).status_code, 403)
        self.client.force_login(self.member)
        self.client.get(url)
        self.assertFalse(BoardCard.objects.get(pk=card.pk).is_pinned)
        self.client.post(url, {'pinned': '1'})
        self.assertTrue(BoardCard.objects.get(pk=card.pk).is_pinned)
        html = self.client.get(fragment_url(self.board)).json()['columns_html']
        self.assertIn('data-card-pinned', html)
        move_card(card, actor=self.member, column=self.column('IN_PROGRESS'))
        self.assertFalse(BoardCard.objects.get(pk=card.pk).is_pinned)
        subtask = create_subtask(card, actor=self.member, title='Позиция')
        with self.assertRaisesMessage(BoardError, 'Подзадачу'):
            set_card_pinned(subtask, actor=self.member, pinned=True)


class CardsFromListTests(RulesFixture, TestCase):
    def test_one_line_one_card_the_presser_empty_lines_skipped(self):
        self.template('Проверить КД')
        with published(self) as publisher:
            cards = create_cards_from_list(
                self.main, actor=self.member, text='Заказ 1\n\n  Заказ 2  \n\nЗаказ 3\n', due_date=due(3),
                column=self.launch().pk,
            )
        self.assertEqual([card.title for card in cards], ['Заказ 1', 'Заказ 2', 'Заказ 3'])
        self.assertEqual([people(card) for card in cards], [['member_one']] * 3)
        self.assertEqual({task_of(card).due_date for card in cards}, {due(3)})
        self.assertEqual({card.column_id for card in cards}, {self.launch().pk})
        self.assertEqual(checklist(cards[0]), ['Проверить КД'], 'create_card()\'s own body: entry rules too')
        self.assertEqual([event.data['change'] for event in board_events(publisher)], ['card_created'])

    def test_limits_refuse_the_whole_list(self):
        before = BoardCard.objects.count()
        with self.assertRaisesMessage(BoardError, 'хотя бы одну строку'):
            create_cards_from_list(self.main, actor=self.member, text='\n  \n', due_date=due())
        with self.assertRaisesMessage(BoardError, f'не больше {MAX_LIST_CARDS}'):
            create_cards_from_list(
                self.main, actor=self.member, text='\n'.join(f'Карточка {i}' for i in range(MAX_LIST_CARDS + 1)),
                due_date=due(),
            )
        with self.assertRaisesMessage(BoardError, 'Строка 2 длиннее'):
            create_cards_from_list(self.main, actor=self.member, text='Коротко\n' + 'х' * 201, due_date=due())
        self.assertEqual(BoardCard.objects.count(), before)

    def test_the_route_and_the_new_card_panel(self):
        url = reverse('boards:card_create_list', args=[self.board.pk, self.main.pk])
        self.client.force_login(self.member)
        panel = self.client.get(board_url(self.board), {'new': self.column('TODO').pk})
        self.assertContains(panel, 'Списком')
        self.assertContains(panel, url)
        self.assertRedirects(self.client.get(url), board_url(self.board), fetch_redirect_response=False)
        response = self.client.post(url, {'text': 'Один\nДва', 'due_date': due().isoformat(), 'column': self.column('TODO').pk})
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        self.assertEqual(BoardCard.objects.filter(board=self.board).count(), 2)
        response = self.client.post(url, {'text': '\n'.join(['x'] * 31), 'due_date': due().isoformat()})
        self.assertContains(response, f'не больше {MAX_LIST_CARDS}', status_code=400)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url, {'text': 'x', 'due_date': due().isoformat()}).status_code, 403)

    def test_update_card_is_untouched_by_entry_rules(self):
        self.template('Проверить КД')
        card = self.card(stage='REVIEW')
        update_card(
            card, actor=self.member, title='Новое', description='', due_date=due(),
            assignee_ids=[self.member.pk],
        )
        self.assertEqual(checklist(card), ['Проверить КД'], 'an edit is no entry')


class ActionQueryCountTests(ActionsFixture, TestCase):
    def count(self, card):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self.client.get(board_url(self.board), {'card': card.pk}).status_code, 200)
        return len(queries)

    def test_one_query_for_the_buttons_whatever_their_number_and_none_without_actions(self):
        card = self.card()
        without = self.count(card)
        self.action('Первое')
        one = self.count(card)
        for index in range(4):
            self.action(f'Ещё {index}')
        self.assertEqual(one, without + 1)
        self.assertEqual(self.count(card), one)
