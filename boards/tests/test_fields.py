"""Stage 15: a board's own card fields — the model, the setup, the values, the display."""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from realtime.events import RealtimeEventType
from realtime.sync import build_sync_state
from realtime.testing import capture_realtime_events

from ..models import BoardCardEvent, BoardCardFieldValue, BoardField, BoardFieldOption
from ..selectors import describe_card_event, format_number
from ..services import (
    MAX_FIELDS,
    MAX_OPTIONS,
    BoardError,
    FieldValueError,
    archive_board,
    archive_field,
    archive_option,
    create_board,
    create_field,
    create_option,
    delete_field,
    delete_option,
    move_field,
    move_option,
    restore_field,
    restore_option,
    update_card,
    update_field,
    update_option,
)
from .helpers import (
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    card_create_url,
    due,
    fragment_url,
    fresh_code,
    make_user,
)
from .test_journal import task_of


S = BoardField.Kind.SELECT
T = BoardField.Kind.TEXT
N = BoardField.Kind.NUMBER
D = BoardField.Kind.DATE


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


def changes(publisher):
    return [event.data['change'] for event in board_events(publisher)]


def values_of(card):
    """`{field name: stored value}` of a card, as the database holds it."""
    result = {}
    for row in BoardCardFieldValue.objects.filter(card=card).select_related('field', 'option'):
        if row.option_id:
            result[row.field.name] = row.option.label
        else:
            result[row.field.name] = next(
                value for value in (row.value_text, row.value_number, row.value_date) if value is not None
            )
    return result


class FieldsMixin(BoardFixtureMixin):
    """The pilot's fields on the fixture board: two texts, a date, two lists."""

    def make_fields(self):
        self.order = create_field(self.board, actor=self.owner, name='Номер заявки', kind=T)
        self.customer = create_field(self.board, actor=self.owner, name='Заказ покупателя', kind=T)
        self.deadline = create_field(self.board, actor=self.owner, name='Срок изг.', kind=D)
        self.priority = create_field(
            self.board, actor=self.owner, name='Приоритет', kind=S,
            options=[('Высокий', 'red'), ('Средний', 'yellow'), ('Низкий', 'gray')],
        )
        self.stop = create_field(self.board, actor=self.owner, name='Стоп', kind=S, options=[('Да', 'red')])
        self.amount = create_field(self.board, actor=self.owner, name='Сумма', kind=N)
        self.high, self.middle, self.low = self.priority.options.order_by('position')
        self.yes = self.stop.options.get()

    def edit(self, card, values, actor=None):
        """`update_card()` keeping everything but the field values."""
        task = task_of(card)
        card.refresh_from_db()
        return update_card(
            card, actor=actor or self.member, title=card.title, description=card.description,
            due_date=task.due_date, assignee_ids=list(task.assignees.values_list('user_id', flat=True)),
            field_values=values,
        )


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


class FieldModelTests(FieldsMixin, TestCase):
    def setUp(self):
        self.make_fields()
        self.card_obj = self.card()

    def test_one_value_per_card_and_field(self):
        BoardCardFieldValue.objects.create(card=self.card_obj, field=self.order, value_text='1')
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardCardFieldValue.objects.create(card=self.card_obj, field=self.order, value_text='2')

    def test_exactly_one_value_column(self):
        cases = {
            'none': {},
            'two': {'value_text': 'x', 'value_number': Decimal('1')},
            'empty text': {'value_text': ''},
            'date and option': {'value_date': datetime.date(2026, 1, 1), 'option': self.high},
        }
        for name, columns in cases.items():
            with self.subTest(case=name), self.assertRaises(IntegrityError), transaction.atomic():
                BoardCardFieldValue.objects.create(card=self.card_obj, field=self.order, **columns)

    def test_clean_checks_the_kind_the_board_and_the_option(self):
        wrong_kind = BoardCardFieldValue(card=self.card_obj, field=self.deadline, value_text='завтра')
        with self.assertRaises(ValidationError):
            wrong_kind.clean()
        other = create_board(code=fresh_code(), name='Другая', owner=self.owner, actor=self.owner)
        foreign = create_field(other, actor=self.owner, name='Чужое', kind=T)
        with self.assertRaises(ValidationError):
            BoardCardFieldValue(card=self.card_obj, field=foreign, value_text='x').clean()
        with self.assertRaises(ValidationError):
            BoardCardFieldValue(card=self.card_obj, field=self.priority, option=self.yes).clean()
        BoardCardFieldValue(card=self.card_obj, field=self.priority, option=self.high).clean()

    def test_an_option_colour_is_one_of_eight(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardFieldOption.objects.create(field=self.priority, label='X', color='pink', position=9)

    def test_a_used_field_or_option_is_protected(self):
        BoardCardFieldValue.objects.create(card=self.card_obj, field=self.priority, option=self.high)
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError):
            self.high.delete()
        with self.assertRaises(ProtectedError):
            self.priority.delete()


# --------------------------------------------------------------------------
# Setting the fields up
# --------------------------------------------------------------------------


class FieldSetupTests(FieldsMixin, TestCase):
    def test_owner_and_administrator_create_one_event_each(self):
        for actor, name in ((self.owner, 'Номер заявки'), (self.admin, 'Заказ покупателя')):
            with self.subTest(actor=actor.username), capture_realtime_events() as publisher:
                with self.captureOnCommitCallbacks(execute=True):
                    field = create_field(self.board, actor=actor, name=f'  {name} ', kind=T)
                self.assertEqual(changes(publisher), ['structure_changed'])
                self.assertEqual(field.name, name)
        self.assertEqual(
            list(BoardField.objects.filter(board=self.board).values_list('name', 'position', 'show_on_tile')),
            [('Номер заявки', 1, True), ('Заказ покупателя', 2, True)],
        )

    def test_a_list_takes_its_options_in_order(self):
        self.make_fields()
        self.assertEqual(
            list(self.priority.options.values_list('label', 'color', 'position')),
            [('Высокий', 'red', 1), ('Средний', 'yellow', 2), ('Низкий', 'gray', 3)],
        )

    def test_member_outsider_and_archived_board_are_refused_with_no_event(self):
        for actor in (self.member, self.outsider):
            with self.subTest(actor=actor.username), capture_realtime_events() as publisher:
                with self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaisesMessage(BoardError, 'владелец доски или администратор'):
                        create_field(self.board, actor=actor, name='Поле', kind=T)
                self.assertEqual(changes(publisher), [])
        board = create_board(code=fresh_code(), name='Архивная', owner=self.owner, actor=self.owner)
        field = create_field(board, actor=self.owner, name='Было', kind=T)
        archive_board(board, actor=self.owner)
        operations = [
            lambda actor: create_field(board, actor=actor, name='Поле', kind=T),
            lambda actor: update_field(field, actor=actor, name='Стало', show_on_tile=True),
            lambda actor: archive_field(field, actor=actor),
            lambda actor: delete_field(field, actor=actor),
        ]
        for actor in (self.owner, self.admin):
            for operation in operations:
                with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
                    operation(actor)
        self.assertEqual(BoardField.objects.get(pk=field.pk).name, 'Было')

    def test_the_name_is_unique_whatever_the_case(self):
        field = create_field(self.board, actor=self.owner, name='Номер заявки', kind=T)
        for name in ('номер заявки', 'НОМЕР ЗАЯВКИ', ' Номер Заявки '):
            with self.subTest(name=name), self.assertRaisesMessage(BoardError, 'уже есть'):
                create_field(self.board, actor=self.owner, name=name, kind=T)
        archive_field(field, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'уже есть'):
            create_field(self.board, actor=self.owner, name='номер заявки', kind=N)
        other = create_field(self.board, actor=self.owner, name='Другое', kind=T)
        with self.assertRaisesMessage(BoardError, 'уже есть'):
            update_field(other, actor=self.owner, name='НОМЕР заявки', show_on_tile=True)
        # The same name on another board is another field.
        board = create_board(code=fresh_code(), name='Вторая', owner=self.owner, actor=self.owner)
        create_field(board, actor=self.owner, name='Номер заявки', kind=T)

    def test_bad_names_kinds_colours_and_options_are_refused(self):
        cases = [
            (dict(name='', kind=T), 'Укажите название поля'),
            (dict(name='я' * 61, kind=T), 'не длиннее 60'),
            (dict(name='Поле', kind='LIST'), 'Неизвестный вид'),
            (dict(name='Поле', kind=T, options=[('Да', 'red')]), 'только у поля вида «Список»'),
            (dict(name='Поле', kind=S, options=[('Да', 'pink')]), 'Неизвестный цвет'),
            (dict(name='Поле', kind=S, options=[('Да', 'red'), ('да', 'blue')]), 'уже есть'),
            (dict(name='Поле', kind=S, options=[('', 'red')]), 'Укажите название варианта'),
            (dict(name='Поле', kind=S, options=[(f'В{i}', 'red') for i in range(MAX_OPTIONS + 1)]),
             f'не больше {MAX_OPTIONS}'),
        ]
        for kwargs, message in cases:
            with self.subTest(kwargs=kwargs), self.assertRaisesMessage(BoardError, message):
                create_field(self.board, actor=self.owner, **kwargs)
        self.assertFalse(BoardField.objects.filter(board=self.board).exists())

    def test_at_most_twenty_live_fields(self):
        fields = [create_field(self.board, actor=self.owner, name=f'Поле {i}', kind=T) for i in range(MAX_FIELDS)]
        with self.assertRaisesMessage(BoardError, f'уже {MAX_FIELDS} полей'):
            create_field(self.board, actor=self.owner, name='Лишнее', kind=T)
        archive_field(fields[0], actor=self.owner)
        create_field(self.board, actor=self.owner, name='Вместо', kind=T)
        # Back from the archive only within the limit.
        with self.assertRaisesMessage(BoardError, f'уже {MAX_FIELDS} полей'):
            restore_field(fields[0], actor=self.owner)
        self.assertTrue(BoardField.objects.get(pk=fields[0].pk).is_archived)

    def test_update_writes_once_and_the_same_values_write_nothing(self):
        self.make_fields()
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                update_field(self.order, actor=self.owner, name='№ заявки', show_on_tile=False)
            with self.captureOnCommitCallbacks(execute=True):
                update_field(self.order, actor=self.owner, name=' № заявки ', show_on_tile=False, kind=T)
            with self.captureOnCommitCallbacks(execute=True):
                update_field(self.order, actor=self.admin, name='№ заявки', show_on_tile=False, kind=None)
        self.assertEqual(changes(publisher), ['structure_changed'])
        self.order.refresh_from_db()
        self.assertEqual((self.order.name, self.order.show_on_tile), ('№ заявки', False))

    def test_the_kind_changes_only_without_values(self):
        self.make_fields()
        update_field(self.priority, actor=self.owner, name='Приоритет', show_on_tile=True, kind=T)
        self.priority.refresh_from_db()
        self.assertEqual(self.priority.kind, T)
        self.assertFalse(self.priority.options.exists(), 'a list turned into text loses its options')
        card = self.card()
        self.edit(card, {self.amount.pk: '5'})
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesMessage(BoardError, 'вид менять нельзя'):
                    update_field(self.amount, actor=self.owner, name='Сумма', show_on_tile=True, kind=T)
        self.assertEqual(changes(publisher), [])
        self.assertEqual(BoardField.objects.get(pk=self.amount.pk).kind, N)

    def test_move_left_and_right_and_the_edges(self):
        self.make_fields()
        move_field(self.customer, actor=self.owner, direction='left')
        names = lambda: list(BoardField.objects.filter(board=self.board).order_by('position').values_list('name', flat=True))  # noqa: E731
        self.assertEqual(names()[:2], ['Заказ покупателя', 'Номер заявки'])
        move_field(self.customer, actor=self.owner, direction='right')
        self.assertEqual(names()[:2], ['Номер заявки', 'Заказ покупателя'])
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesMessage(BoardError, 'уже первое'):
                    move_field(self.order, actor=self.owner, direction='left')
                with self.assertRaisesMessage(BoardError, 'уже последнее'):
                    move_field(self.amount, actor=self.owner, direction='right')
        self.assertEqual(changes(publisher), [])

    def test_archive_and_restore_once_each(self):
        self.make_fields()
        with capture_realtime_events() as publisher:
            for operation in (archive_field, archive_field, restore_field, restore_field):
                with self.captureOnCommitCallbacks(execute=True):
                    operation(self.order, actor=self.owner)
        self.assertEqual(changes(publisher), ['structure_changed', 'structure_changed'])
        self.assertFalse(BoardField.objects.get(pk=self.order.pk).is_archived)

    def test_delete_only_an_unused_field(self):
        self.make_fields()
        delete_field(self.priority, actor=self.owner)
        self.assertFalse(BoardField.objects.filter(pk=self.priority.pk).exists())
        self.assertFalse(BoardFieldOption.objects.filter(pk=self.high.pk).exists())
        self.assertEqual(
            list(BoardField.objects.filter(board=self.board).order_by('position').values_list('position', flat=True)),
            [1, 2, 3, 4, 5],
        )
        card = self.card()
        self.edit(card, {self.order.pk: '17'})
        with self.assertRaisesMessage(BoardError, 'можно только убрать в архив'):
            delete_field(self.order, actor=self.owner)
        archive_field(self.order, actor=self.owner)
        self.assertEqual(values_of(card), {'Номер заявки': '17'})

    def test_options_create_update_move_archive_restore_delete(self):
        self.make_fields()
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                urgent = create_option(self.priority, actor=self.owner, label='Срочно', color='orange')
            with self.captureOnCommitCallbacks(execute=True):
                update_option(urgent, actor=self.owner, label='Срочно', color='orange')  # the same: nothing
            with self.captureOnCommitCallbacks(execute=True):
                update_option(urgent, actor=self.admin, label='Очень срочно', color='purple')
            with self.captureOnCommitCallbacks(execute=True):
                move_option(urgent, actor=self.owner, direction='left')
            with self.captureOnCommitCallbacks(execute=True):
                archive_option(urgent, actor=self.owner)
            with self.captureOnCommitCallbacks(execute=True):
                restore_option(urgent, actor=self.owner)
            with self.captureOnCommitCallbacks(execute=True):
                delete_option(urgent, actor=self.owner)
        self.assertEqual(changes(publisher), ['structure_changed'] * 6)
        self.assertEqual(
            list(self.priority.options.order_by('position').values_list('label', 'position')),
            [('Высокий', 1), ('Средний', 2), ('Низкий', 3)],
        )

    def test_option_refusals(self):
        self.make_fields()
        cases = [
            (lambda: create_option(self.order, actor=self.owner, label='Да', color='red'), 'только у поля вида «Список»'),
            (lambda: create_option(self.priority, actor=self.owner, label='высокий', color='red'), 'уже есть'),
            (lambda: create_option(self.priority, actor=self.owner, label='Новый', color='pink'), 'Неизвестный цвет'),
            (lambda: create_option(self.priority, actor=self.member, label='Новый', color='red'), 'владелец доски'),
            (lambda: update_option(self.middle, actor=self.owner, label='Низкий', color='red'), 'уже есть'),
            (lambda: move_option(self.high, actor=self.owner, direction='left'), 'уже первый'),
            (lambda: move_option(self.low, actor=self.owner, direction='right'), 'уже последний'),
        ]
        with capture_realtime_events() as publisher:
            for operation, message in cases:
                with self.subTest(message=message), self.captureOnCommitCallbacks(execute=True):
                    with self.assertRaisesMessage(BoardError, message):
                        operation()
        self.assertEqual(changes(publisher), [])

    def test_at_most_thirty_live_options(self):
        field = create_field(
            self.board, actor=self.owner, name='Много', kind=S,
            options=[(f'В{i}', 'gray') for i in range(MAX_OPTIONS)],
        )
        with self.assertRaisesMessage(BoardError, f'не больше {MAX_OPTIONS}'):
            create_option(field, actor=self.owner, label='Лишний', color='red')
        first = field.options.order_by('position').first()
        archive_option(first, actor=self.owner)
        create_option(field, actor=self.owner, label='Вместо', color='red')
        with self.assertRaisesMessage(BoardError, f'не больше {MAX_OPTIONS}'):
            restore_option(first, actor=self.owner)

    def test_a_used_option_is_only_archived(self):
        self.make_fields()
        card = self.card()
        self.edit(card, {self.priority.pk: self.high.pk})
        with self.assertRaisesMessage(BoardError, 'можно только убрать в архив'):
            delete_option(self.high, actor=self.owner)
        archive_option(self.high, actor=self.owner)
        self.assertEqual(values_of(card), {'Приоритет': 'Высокий'})

    def test_a_setup_change_moves_the_sync_revision(self):
        before = build_sync_state(self.owner)['revisions']['boards']
        field = create_field(self.board, actor=self.owner, name='Номер заявки', kind=T)
        after_create = build_sync_state(self.owner)['revisions']['boards']
        self.assertNotEqual(before, after_create)
        update_field(field, actor=self.owner, name='Номер', show_on_tile=True)
        self.assertNotEqual(after_create, build_sync_state(self.owner)['revisions']['boards'])


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------


class FieldValueTests(FieldsMixin, TestCase):
    def setUp(self):
        self.make_fields()

    def test_create_card_parses_every_kind(self):
        card = self.card(field_values={
            self.order.pk: '  З-17 ',
            self.deadline.pk: '2026-11-30',
            self.priority.pk: str(self.high.pk),
            self.amount.pk: '1 234,5',
            self.customer.pk: '',
            self.stop.pk: None,
        })
        self.assertEqual(values_of(card), {
            'Номер заявки': 'З-17',
            'Срок изг.': datetime.date(2026, 11, 30),
            'Приоритет': 'Высокий',
            'Сумма': Decimal('1234.5'),
        })
        # Nothing about the fields in the card's creation entry.
        self.assertEqual(list(card.events.values_list('kind', flat=True)), ['CREATED'])

    def test_numbers(self):
        card = self.card()
        accepted = {
            '12': Decimal('12'), '-3,5': Decimal('-3.5'), '+7.25': Decimal('7.25'), ',5': Decimal('0.5'),
            '1 000 000': Decimal('1000000'), '9' * 14: Decimal('9' * 14),
            '0,1234': Decimal('0.1234'), '1,50000': Decimal('1.5'), '-0': Decimal('0'),
        }
        for raw, expected in accepted.items():
            with self.subTest(raw=raw):
                self.edit(card, {self.amount.pk: raw})
                self.assertEqual(values_of(card).get('Сумма'), expected)
        refused = {
            'abc': 'введите число', '1e5': 'введите число', '1,2,3': 'введите число', 'NaN': 'введите число',
            '1' * 15: 'не больше 14 цифр до запятой', '0,12345': 'не больше 4 цифр после запятой',
        }
        for raw, message in refused.items():
            with self.subTest(raw=raw), self.assertRaisesMessage(FieldValueError, f'«Сумма»: {message}'):
                self.edit(card, {self.amount.pk: raw})

    def test_dates(self):
        card = self.card()
        self.edit(card, {self.deadline.pk: datetime.date(2026, 3, 1)})
        self.assertEqual(values_of(card)['Срок изг.'], datetime.date(2026, 3, 1))
        for raw in ('2026-13-01', '2026-02-30', '01.02.2026', 'завтра', '2026-2-1'):
            with self.subTest(raw=raw), self.assertRaisesMessage(FieldValueError, '«Срок изг.»: дата'):
                self.edit(card, {self.deadline.pk: raw})
        self.assertEqual(values_of(card)['Срок изг.'], datetime.date(2026, 3, 1))

    def test_text_is_trimmed_and_at_most_500(self):
        card = self.card()
        self.edit(card, {self.order.pk: 'я' * 500})
        with self.assertRaisesMessage(FieldValueError, '«Номер заявки»: не длиннее 500'):
            self.edit(card, {self.order.pk: 'я' * 501})

    def test_options_of_another_field_and_archived_ones_are_refused(self):
        card = self.card()
        for raw in (self.yes.pk, 'abc', 999999):
            with self.subTest(raw=raw), self.assertRaisesMessage(FieldValueError, '«Приоритет»: выберите вариант'):
                self.edit(card, {self.priority.pk: raw})
        archive_option(self.low, actor=self.owner)
        with self.assertRaisesMessage(FieldValueError, 'вариант «Низкий» убран в архив'):
            self.edit(card, {self.priority.pk: self.low.pk})

    def test_an_archived_option_the_card_holds_is_kept_when_sent_back(self):
        card = self.card(field_values={self.priority.pk: self.low.pk})
        archive_option(self.low, actor=self.owner)
        version = type(card).objects.get(pk=card.pk).version
        self.edit(card, {self.priority.pk: self.low.pk})
        card.refresh_from_db()
        self.assertEqual(card.version, version, 'sent back unchanged: nothing stored')
        self.assertEqual(values_of(card), {'Приоритет': 'Низкий'})

    def test_the_error_names_the_field_and_says_which(self):
        card = self.card()
        with self.assertRaises(FieldValueError) as caught:
            self.edit(card, {self.amount.pk: 'много'})
        self.assertEqual(caught.exception.field_id, self.amount.pk)
        self.assertIn('«Сумма»', str(caught.exception))

    def test_a_refusal_writes_nothing(self):
        card = self.card()
        with self.assertRaises(FieldValueError):
            self.edit(card, {self.order.pk: 'Новый', self.amount.pk: 'много'})
        self.assertEqual(values_of(card), {})
        self.assertEqual(list(card.events.values_list('kind', flat=True)), ['CREATED'])

    def test_empty_deletes_the_row(self):
        card = self.card(field_values={self.order.pk: '17', self.priority.pk: self.high.pk})
        self.edit(card, {self.order.pk: '   ', self.priority.pk: ''})
        self.assertEqual(values_of(card), {})
        self.assertFalse(BoardCardFieldValue.objects.filter(card=card).exists())

    def test_a_field_of_another_board_is_refused(self):
        other = create_board(code=fresh_code(), name='Другая', owner=self.owner, actor=self.owner)
        foreign = create_field(other, actor=self.owner, name='Чужое', kind=T)
        card = self.card()
        for key in (foreign.pk, 999999, 'abc'):
            with self.subTest(key=key), self.assertRaisesMessage(BoardError, 'не найдено на этой доске'):
                self.edit(card, {key: 'x'})
        with self.assertRaisesMessage(BoardError, 'не найдено на этой доске'):
            self.card(field_values={foreign.pk: 'x'})

    def test_an_archived_field_is_left_as_it_is(self):
        card = self.card(field_values={self.order.pk: '17'})
        archive_field(self.order, actor=self.owner)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.edit(card, {self.order.pk: 'другое'})
                self.edit(card, {self.order.pk: ''})
        self.assertEqual(changes(publisher), [])
        self.assertEqual(values_of(card), {'Номер заявки': '17'})
        # Nor may a new card start one.
        fresh = self.card(field_values={self.order.pk: '18'})
        self.assertEqual(values_of(fresh), {})

    def test_an_edit_of_values_is_one_journal_entry_one_version_one_event(self):
        card = self.card()
        version = card.version
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.edit(card, {self.deadline.pk: '2026-12-01', self.order.pk: 'З-1'})
        self.assertEqual(changes(publisher), ['card_updated'])
        card.refresh_from_db()
        self.assertEqual(card.version, version + 1)
        event = card.events.get(kind=BoardCardEvent.Kind.EDITED)
        # The fields' order, not the order they were sent in.
        self.assertEqual(event.details, {'fields': ['custom'], 'custom_fields': ['Номер заявки', 'Срок изг.']})
        self.assertEqual(describe_card_event(event), 'Изменено: Номер заявки, Срок изг.')

    def test_the_journal_keeps_the_names_as_they_were(self):
        card = self.card()
        task = task_of(card)
        update_card(
            card, actor=self.member, title='Новый заголовок', description='', due_date=task.due_date,
            assignee_ids=[self.member.pk], field_values={self.priority.pk: self.middle.pk},
        )
        update_field(self.priority, actor=self.owner, name='Важность', show_on_tile=True)
        event = card.events.get(kind=BoardCardEvent.Kind.EDITED)
        self.assertEqual(event.details['fields'], ['title', 'custom'])
        self.assertEqual(describe_card_event(event), 'Изменено: заголовок, Приоритет')

    def test_an_edit_that_changes_nothing_writes_nothing(self):
        card = self.card(field_values={
            self.order.pk: '17', self.amount.pk: '3,50', self.deadline.pk: '2026-12-01',
            self.priority.pk: self.high.pk,
        })
        version = type(card).objects.get(pk=card.pk).version
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.edit(card, {
                    self.order.pk: ' 17 ', self.amount.pk: '3.5', self.deadline.pk: '2026-12-01',
                    self.priority.pk: str(self.high.pk), self.customer.pk: '',
                })
        self.assertEqual(changes(publisher), [])
        card.refresh_from_db()
        self.assertEqual(card.version, version)
        self.assertEqual(list(card.events.values_list('kind', flat=True)), ['CREATED'])

    def test_field_values_none_touches_no_field(self):
        card = self.card(field_values={self.order.pk: '17'})
        task = task_of(card)
        update_card(
            card, actor=self.member, title='Другой', description='', due_date=task.due_date,
            assignee_ids=[self.member.pk],
        )
        self.assertEqual(values_of(card), {'Номер заявки': '17'})

    def test_a_stale_version_stores_no_value(self):
        from ..services import StaleCardError

        card = self.card()
        task = task_of(card)
        with self.assertRaises(StaleCardError):
            update_card(
                card, actor=self.member, title=card.title, description='', due_date=task.due_date,
                assignee_ids=[self.member.pk], expected_version=card.version + 5,
                field_values={self.order.pk: '17'},
            )
        self.assertEqual(values_of(card), {})


# --------------------------------------------------------------------------
# Display: the tile, «Описание», the form, the fragment, the queries
# --------------------------------------------------------------------------


class FieldDisplayTests(FieldsMixin, TestCase):
    def setUp(self):
        self.make_fields()
        self.card_obj = self.card('Заказ 7', field_values={
            self.order.pk: 'З-17',
            self.customer.pk: 'ООО «Ромашка»',
            self.deadline.pk: '2026-11-30',
            self.priority.pk: self.high.pk,
            self.stop.pk: self.yes.pk,
            self.amount.pk: '1234567,50',
        })
        self.client.force_login(self.member)

    def fragment(self, **query):
        return self.client.get(fragment_url(self.board), query).json()

    def test_the_tile_shows_four_in_field_order(self):
        html = self.fragment()['columns_html']
        self.assertIn('title="Номер заявки: З-17">Номер заявки: З-17<', html)
        self.assertIn('title="Заказ покупателя: ООО «Ромашка»"', html.replace('&laquo;', '«').replace('&raquo;', '»'))
        self.assertIn('title="Срок изг.: 30.11.2026"', html)
        self.assertIn('board-chip--red text-ellipsis" title="Приоритет: Высокий">Высокий<', html)
        # The fifth and the sixth are not on the tile.
        self.assertNotIn('Стоп: Да', html)
        self.assertNotIn('Сумма', html)
        positions = [html.index(text) for text in ('Номер заявки: З-17', 'Срок изг.: 30.11.2026', '>Высокий<')]
        self.assertEqual(positions, sorted(positions))

    def test_show_on_tile_and_the_archive_leave_room_for_the_next(self):
        update_field(self.order, actor=self.owner, name='Номер заявки', show_on_tile=False)
        archive_field(self.customer, actor=self.owner)
        archive_option(self.high, actor=self.owner)
        html = self.fragment()['columns_html']
        self.assertNotIn('Номер заявки', html)
        self.assertNotIn('Заказ покупателя', html)
        self.assertNotIn('>Высокий<', html)
        self.assertIn('title="Стоп: Да">Да<', html)
        self.assertIn('title="Сумма: 1 234 567,5"', html)

    def test_description_shows_every_value_formatted_and_the_archived_marked(self):
        archive_field(self.customer, actor=self.owner)
        archive_option(self.yes, actor=self.owner)
        html = self.fragment(card=self.card_obj.pk)['facts_html']
        expected = [
            'Номер заявки</dt><dd><span class="user-text">З-17</span></dd>',
            'Заказ покупателя</dt><dd><span class="user-text">ООО «Ромашка»</span> <span class="board-drawer__archived">(в архиве)</span>',
            'Срок изг.</dt><dd><span class="user-text">30.11.2026</span>',
            'Приоритет</dt><dd><span class="board-chip board-chip--red">Высокий</span></dd>',
            'Стоп</dt><dd><span class="board-chip board-chip--red">Да</span> <span class="board-drawer__archived">(в архиве)</span>',
            'Сумма</dt><dd><span class="user-text">1 234 567,5</span>',
        ]
        html = html.replace('&laquo;', '«').replace('&raquo;', '»')
        positions = []
        for text in expected:
            with self.subTest(text=text):
                self.assertIn(text, html)
                positions.append(html.index(text))
        self.assertEqual(positions, sorted(positions))

    def test_an_empty_value_is_not_shown(self):
        card = self.card('Пустая', field_values={self.order.pk: '5'})
        html = self.fragment(card=card.pk)['facts_html']
        self.assertIn('Номер заявки</dt>', html)
        self.assertNotIn('Срок изг.</dt>', html)
        self.assertNotIn('Приоритет</dt>', html)

    def test_number_formatting(self):
        cases = {
            '1234567.5': '1 234 567,5', '1000': '1 000', '-1234.25': '-1 234,25',
            '0.5000': '0,5', '100': '100', '12.0000': '12', '0': '0', '99999999999999.9999': '99 999 999 999 999,9999',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(format_number(Decimal(raw)), expected)

    def test_the_edit_form_offers_the_live_fields_with_the_current_values(self):
        archive_field(self.customer, actor=self.owner)
        archive_option(self.high, actor=self.owner)
        html = self.fragment(card=self.card_obj.pk, edit='1')['card_html']
        self.assertIn(f'name="field_{self.order.pk}" value="З-17"', html)
        self.assertNotIn(f'name="field_{self.customer.pk}"', html)
        self.assertIn(f'type="date" name="field_{self.deadline.pk}" value="2026-11-30"', html)
        self.assertIn(f'name="field_{self.amount.pk}" value="1234567,5"', html)
        self.assertIn('inputmode="decimal"', html)
        # The archived option the card holds is still offered, marked — so a
        # save does not drop it — and another archived one is not.
        self.assertIn(f'value="{self.high.pk}" selected class="board-option board-option--red" data-color="red">● Высокий (в архиве)<', html)
        self.assertIn(f'>● Средний<', html)

    def test_the_new_card_form_offers_the_live_fields_and_no_archived_option(self):
        archive_option(self.low, actor=self.owner)
        html = self.fragment(new=self.column().pk)['card_html']
        for field in (self.order, self.customer, self.deadline, self.priority, self.stop, self.amount):
            self.assertIn(f'name="field_{field.pk}"', html)
        self.assertNotIn('Низкий', html)
        self.assertIn('<option value="" selected>—</option>', html)

    def test_create_and_edit_through_the_forms(self):
        response = self.client.post(card_create_url(self.board), {
            'title': 'Из формы', 'due_date': due().isoformat(), 'assignees': [self.member.pk],
            'column': self.column().pk,
            f'field_{self.order.pk}': 'З-99', f'field_{self.priority.pk}': str(self.middle.pk),
            f'field_{self.amount.pk}': '7,5',
        })
        self.assertEqual(response.status_code, 302)
        card = type(self.card_obj).objects.get(title='Из формы')
        self.assertEqual(values_of(card), {'Номер заявки': 'З-99', 'Приоритет': 'Средний', 'Сумма': Decimal('7.5')})
        response = self.client.post(reverse('boards:card_update', args=[self.board.pk, card.pk]), {
            'title': 'Из формы', 'due_date': due().isoformat(), 'assignees': [self.member.pk],
            'version': card.version,
            f'field_{self.order.pk}': '', f'field_{self.priority.pk}': str(self.low.pk),
            f'field_{self.amount.pk}': '7,5', f'field_{self.deadline.pk}': '2027-01-15',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(values_of(card), {
            'Приоритет': 'Низкий', 'Сумма': Decimal('7.5'), 'Срок изг.': datetime.date(2027, 1, 15),
        })
        event = card.events.get(kind=BoardCardEvent.Kind.EDITED)
        self.assertEqual(event.details['custom_fields'], ['Номер заявки', 'Срок изг.', 'Приоритет'])

    def test_a_refused_value_keeps_everything_typed_and_names_the_field_beside_it(self):
        response = self.client.post(card_create_url(self.board), {
            'title': 'С ошибкой', 'due_date': due().isoformat(), 'assignees': [self.member.pk],
            'column': self.column().pk,
            f'field_{self.order.pk}': 'З-5', f'field_{self.amount.pk}': 'двенадцать',
            f'field_{self.priority.pk}': str(self.middle.pk),
        })
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('«Сумма»: введите число', html)
        self.assertIn(f'name="field_{self.amount.pk}" value="двенадцать"', html)
        self.assertIn(f'name="field_{self.order.pk}" value="З-5"', html)
        self.assertIn(f'value="{self.middle.pk}" selected class="board-option board-option--yellow" data-color="yellow">● Средний<', html)
        self.assertFalse(type(self.card_obj).objects.filter(title='С ошибкой').exists())

    def test_the_page_matches_its_fragment(self):
        for query in ({}, {'card': self.card_obj.pk}, {'card': self.card_obj.pk, 'edit': '1'}, {'new': self.column().pk}):
            with self.subTest(query=query):
                page = self.client.get(board_url(self.board), query).content.decode()
                assert_page_matches_fragment(
                    self, page, self.fragment(**query),
                    blocks={
                        'tabs': ('tabs_html',), 'columns': ('columns_html',),
                        'panel': ('panel_html', 'card_html', 'facts_html'),
                    },
                )

    def test_the_columns_fingerprint_follows_tile_values_and_the_setup(self):
        first = self.fragment()['columns_revision']
        # A value no tile shows: the columns stay.
        self.edit(self.card_obj, {self.stop.pk: ''})
        self.assertEqual(self.fragment()['columns_revision'], first)
        # A value on the tile.
        self.edit(self.card_obj, {self.order.pk: 'З-18'})
        second = self.fragment()['columns_revision']
        self.assertNotEqual(second, first)
        # Any setup change, even one no tile shows.
        create_field(self.board, actor=self.owner, name='Комментарий', kind=T)
        third = self.fragment()['columns_revision']
        self.assertNotEqual(third, second)
        update_option(self.high, actor=self.owner, label='Высокий', color='orange')
        self.assertNotEqual(self.fragment()['columns_revision'], third)

    def test_a_value_change_moves_the_panel_and_a_message_does_not(self):
        before = self.fragment(card=self.card_obj.pk)
        self.edit(self.card_obj, {self.amount.pk: '1'})
        after = self.fragment(card=self.card_obj.pk)
        self.assertNotEqual(before['panel_revision'], after['panel_revision'])

    def test_the_fields_page_link_is_the_managers(self):
        link = reverse('boards:fields', args=[self.board.pk])
        self.assertNotIn(link, self.client.get(board_url(self.board)).content.decode())
        self.client.force_login(self.owner)
        self.assertIn(link, self.client.get(board_url(self.board)).content.decode())


class FieldQueryCountTests(FieldsMixin, TestCase):
    """The page and its fragment cost the same however many fields, options
    and values the board has: the fields with their options are two queries,
    the values of every card on the page one."""

    # A sub-board page with a card open, for an исполнитель of it, on a board
    # with card fields (`boards/tests/test_journal.py` measures one without);
    # two more since the card's «Чек-лист» and its followers are read; at
    # stage 19 one more for the files of «Чат» and two fewer (the member
    # count and the tabs read once); at stage 20 two more for the card's
    # «Подзадачи» (the subtasks with their tasks, and their исполнители); at
    # stage 21 one more for the card's «Переносы» (the moves of its срок); at
    # stage 22 two more: the columns this reader follows and the card's «Связи»;
    # at stage 23 one more: the reader's «Дайджест на почту»; at stage 24 two
    # more: «Входящие (N)» in the tabs and the menu's lazy «Заявки».
    PAGE_QUERIES = 46

    def setUp(self):
        self.make_fields()
        self.card_obj = self.card('Открытая', field_values={self.order.pk: '1', self.priority.pk: self.high.pk})
        self.card('Вторая', field_values={self.deadline.pk: '2026-01-01'})
        self.client.force_login(self.member)

    def count(self, url, query):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(url, query)
        return len(queries)

    def counts(self):
        query = {'card': self.card_obj.pk}
        return (
            self.count(board_url(self.board), query),
            self.count(fragment_url(self.board), query),
            self.count(fragment_url(self.board), {'card': self.card_obj.pk, 'edit': '1'}),
        )

    def test_the_count_does_not_grow_with_fields_options_or_values(self):
        baseline = self.counts()
        self.assertEqual(baseline[0], self.PAGE_QUERIES)
        for index in range(4):
            field = create_field(
                self.board, actor=self.owner, name=f'Ещё {index}', kind=S,
                options=[(f'В{index}-{i}', 'blue') for i in range(5)],
            )
            create_field(self.board, actor=self.owner, name=f'Текст {index}', kind=T)
            option = field.options.first()
            for card_index in range(3):
                self.card(f'Карточка {index}-{card_index}', field_values={field.pk: option.pk, self.order.pk: str(card_index)})
            self.edit(self.card_obj, {field.pk: option.pk})
        archive_field(self.customer, actor=self.owner)
        archive_option(self.low, actor=self.owner)
        self.assertEqual(self.counts(), baseline)


class QuickSearchQueryTests(FieldsMixin, TestCase):
    def test_hits_of_board_cards_cost_no_query_each(self):
        from dashboard.search import quick_search

        def run():
            user = type(self.member).objects.get(pk=self.member.pk)
            with CaptureQueriesContext(connection) as queries:
                hits = dict(quick_search(user, 'Заказ'))['Задачи']
            return len(queries), len(hits)

        self.card('Заказ 1')
        baseline, found = run()
        self.assertEqual(found, 1)
        for index in range(2, 7):
            self.card(f'Заказ {index}')
        count, found = run()
        self.assertEqual(found, 6)
        self.assertEqual(count, baseline)


# --------------------------------------------------------------------------
# «Поля карточек»
# --------------------------------------------------------------------------


class FieldsPageTests(FieldsMixin, TestCase):
    def setUp(self):
        self.make_fields()
        self.page = reverse('boards:fields', args=[self.board.pk])

    def test_a_reader_reads_the_list_without_forms(self):
        card = self.card(field_values={self.priority.pk: self.high.pk})
        archive_field(self.stop, actor=self.owner)
        self.client.force_login(self.member)
        response = self.client.get(self.page)
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        for text in ('Номер заявки', 'Срок изг.', 'Приоритет', '>Список<', '>Дата<', 'В архиве',
                     'board-chip--red', 'Высокий', 'Средний'):
            self.assertIn(text, html)
        self.assertNotIn(reverse('boards:field_update', args=[self.board.pk, self.priority.pk]), html)
        self.assertNotIn(reverse('boards:field_create', args=[self.board.pk]), html)
        self.assertNotIn('Изменить', html)
        self.assertIn('<dt>Карточек со значением</dt><dd>1</dd>', html)
        self.assertIsNotNone(card)

    def test_the_manager_gets_the_forms_and_the_counts(self):
        self.card(field_values={self.priority.pk: self.high.pk, self.order.pk: '1'})
        self.card(field_values={self.priority.pk: self.high.pk})
        self.client.force_login(self.owner)
        html = self.client.get(self.page).content.decode()
        self.assertIn('+ Поле', html)
        self.assertIn(reverse('boards:field_update', args=[self.board.pk, self.priority.pk]), html)
        # A used field offers no kind and no «Удалить»; an unused one both.
        self.assertNotIn(reverse('boards:field_delete', args=[self.board.pk, self.priority.pk]), html)
        self.assertIn(reverse('boards:field_delete', args=[self.board.pk, self.customer.pk]), html)
        self.assertNotIn(reverse('boards:option_delete', args=[self.board.pk, self.priority.pk, self.high.pk]), html)
        self.assertIn(reverse('boards:option_delete', args=[self.board.pk, self.priority.pk, self.low.pk]), html)
        self.assertIn('<dt>Карточек со значением</dt><dd>2</dd>', html)

    def test_an_archived_board_reads_without_forms(self):
        board = create_board(code=fresh_code(), name='Архивная', owner=self.owner, actor=self.owner)
        create_field(board, actor=self.owner, name='Поле', kind=T)
        archive_board(board, actor=self.owner)
        self.client.force_login(self.owner)
        html = self.client.get(reverse('boards:fields', args=[board.pk])).content.decode()
        self.assertIn('Поле', html)
        self.assertNotIn('+ Поле', html)

    def routes(self):
        b, f, o = self.board.pk, self.priority.pk, self.middle.pk
        return [
            (reverse('boards:field_create', args=[b]), {'name': 'Новое', 'kind': T}),
            (reverse('boards:field_update', args=[b, f]), {'name': 'Важность', 'show_on_tile': '1'}),
            (reverse('boards:field_move', args=[b, f]), {'direction': 'left'}),
            (reverse('boards:field_archive', args=[b, f]), {}),
            (reverse('boards:field_restore', args=[b, f]), {}),
            (reverse('boards:option_create', args=[b, f]), {'label': 'Срочно', 'color': 'orange'}),
            (reverse('boards:option_update', args=[b, f, o]), {'label': 'Средний+', 'color': 'blue'}),
            (reverse('boards:option_move', args=[b, f, o]), {'direction': 'left'}),
            (reverse('boards:option_archive', args=[b, f, o]), {}),
            (reverse('boards:option_restore', args=[b, f, o]), {}),
            (reverse('boards:option_delete', args=[b, f, o]), {}),
            (reverse('boards:field_delete', args=[b, self.customer.pk]), {}),
        ]

    def snapshot(self):
        return (
            list(BoardField.objects.filter(board=self.board).values_list('name', 'position', 'is_archived', 'show_on_tile')),
            list(BoardFieldOption.objects.filter(field__board=self.board).values_list('label', 'color', 'position', 'is_archived')),
        )

    def test_routes_are_403_before_the_method_for_a_member(self):
        self.client.force_login(self.member)
        before = self.snapshot()
        for url, data in self.routes():
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_a_get_changes_nothing(self):
        self.client.force_login(self.owner)
        before = self.snapshot()
        for url, _data in self.routes():
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response['Location'].startswith(self.page))
        self.assertEqual(self.snapshot(), before)

    def test_every_route_works_for_the_owner(self):
        self.client.force_login(self.owner)
        for url, data in self.routes():
            with self.subTest(url=url):
                response = self.client.post(url, data)
                self.assertEqual(response.status_code, 302, url)
        fields = dict(BoardField.objects.filter(board=self.board).values_list('name', 'show_on_tile'))
        self.assertIn('Новое', fields)
        self.assertIn('Важность', fields)
        self.assertNotIn('Заказ покупателя', fields)
        importance = BoardField.objects.get(board=self.board, name='Важность')
        # Moved one place left of «Срок изг.», then «Заказ покупателя» deleted.
        self.assertEqual(
            list(BoardField.objects.filter(board=self.board).order_by('position').values_list('name', flat=True)),
            ['Номер заявки', 'Важность', 'Срок изг.', 'Стоп', 'Сумма', 'Новое'],
        )
        self.assertEqual(
            list(importance.options.order_by('position').values_list('label', 'color')),
            [('Высокий', 'red'), ('Низкий', 'gray'), ('Срочно', 'orange')],
        )

    def test_a_refusal_is_a_message_on_the_page(self):
        self.card(field_values={self.priority.pk: self.high.pk})
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('boards:field_update', args=[self.board.pk, self.priority.pk]),
            {'name': 'Приоритет', 'kind': T}, follow=True,
        )
        self.assertContains(response, 'вид менять нельзя')
        response = self.client.post(
            reverse('boards:option_delete', args=[self.board.pk, self.priority.pk, self.high.pk]), follow=True,
        )
        self.assertContains(response, 'можно только убрать в архив')

    def test_create_a_list_with_options_from_rows(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('boards:field_create', args=[self.board.pk]), {
            'name': 'Цех', 'kind': S,
            'option_label': ['МП', '', 'ПиР'], 'option_color': ['blue', 'red', 'green'],
        })
        self.assertEqual(response.status_code, 302)
        field = BoardField.objects.get(board=self.board, name='Цех')
        self.assertEqual(list(field.options.values_list('label', 'color')), [('МП', 'blue'), ('ПиР', 'green')])

    def test_options_sent_for_another_kind_are_dropped(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('boards:field_create', args=[self.board.pk]), {
            'name': 'Примечание', 'kind': T, 'option_label': ['Лишний'], 'option_color': ['red'],
        })
        field = BoardField.objects.get(board=self.board, name='Примечание')
        self.assertFalse(field.options.exists())

    def test_add_option_row_without_javascript_writes_nothing(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('boards:field_create', args=[self.board.pk]), {
            'name': 'Цех', 'kind': S, 'option_label': ['МП'], 'option_color': ['blue'], 'add_option_row': '1',
        })
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertEqual(html.count('data-option-row>'), 3, 'two rows and the template')
        self.assertIn('value="МП"', html)
        self.assertIn('value="Цех"', html)
        self.assertFalse(BoardField.objects.filter(board=self.board, name='Цех').exists())

    def test_a_refused_creation_keeps_what_was_typed(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('boards:field_create', args=[self.board.pk]), {
            'name': 'приоритет', 'kind': S, 'option_label': ['А'], 'option_color': ['red'],
        })
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('Поле «приоритет» на этой доске уже есть.', html)
        self.assertIn('value="приоритет"', html)
        self.assertIn('value="А"', html)


class FieldsPageAccessTests(TestCase):
    """The real rule: a board's fields page is its readers'."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = make_user('fields_admin', UserProfile.Role.ADMIN)
        cls.member = make_user('fields_member', UserProfile.Role.OTK)
        cls.stranger = make_user('fields_stranger', UserProfile.Role.PDO)
        cls.board = create_board(
            code=fresh_code(), name='Доска', owner=cls.admin, actor=cls.admin, member_ids=[cls.member.pk],
        )
        cls.field = create_field(cls.board, actor=cls.admin, name='Номер заявки', kind=T)

    def test_a_non_reader_is_refused(self):
        page = reverse('boards:fields', args=[self.board.pk])
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(page).status_code, 403)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(page).status_code, 200)
        self.assertEqual(
            self.client.post(reverse('boards:field_archive', args=[self.board.pk, self.field.pk])).status_code, 403,
        )
        self.client.force_login(self.admin)
        self.client.post(reverse('boards:field_archive', args=[self.board.pk, self.field.pk]))
        self.assertTrue(BoardField.objects.get(pk=self.field.pk).is_archived)
