"""«Сроки и отклонения»: the reasons, a move of a card's срок and its
history, the reminders of `board_due_reminders`, what the board, the table
and its Excel show, the «Отклонения» report — and the stage-20 follow-up:
a subtask's completion is told to its followers, not its author."""

import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from ecosystem.workdays import add_working_days
from notifications.models import Notification
from notifications.services import EMAIL_ELIGIBLE_EVENTS, due_day_words
from references.models import DeviationReason
from tasks.models import Task

from ..models import Board, BoardCardDueChange, BoardCardEvent
from ..selectors import (
    build_board_state,
    build_board_table,
    build_deviation_report,
    due_change_hint,
    due_change_label,
)
from ..services import (
    DueReasonError,
    StaleCardError,
    complete_card,
    create_sub_board,
    create_subtask,
    send_due_reminders,
    toggle_card_subscription,
    update_card,
)
from .helpers import BoardFixtureMixin, board_url, column_of, due, new_card, reason_id
from .test_journal import task_of
from .test_table import read_xlsx, values


def assignee_ids(card):
    return list(task_of(card).assignees.values_list('user_id', flat=True))


def notes(event_type):
    return Notification.objects.filter(event_type=event_type).order_by('recipient_id', 'pk')


class DueMixin(BoardFixtureMixin):
    def move(self, card, due_date, *, reason='MATERIAL', comment='', actor=None, **extra):
        """Correct only the срок of `card`, naming `reason` (a code, or None)."""
        card.refresh_from_db()
        return update_card(
            card,
            actor=actor or self.member,
            title=card.title,
            description=card.description,
            due_date=due_date,
            assignee_ids=assignee_ids(card),
            due_reason_id=reason_id(reason) if reason else None,
            due_comment=comment,
            **extra,
        )

    def set_due(self, card, due_date):
        """Put a card's срок where a test needs it — in the past too — the
        way no service would: straight on the task."""
        Task.objects.filter(board_card=card).update(due_date=due_date)


# --------------------------------------------------------------------------
# The reference
# --------------------------------------------------------------------------


class DeviationReasonOfferTests(DueMixin, TestCase):
    def test_the_edit_form_offers_the_active_reasons_in_order(self):
        card = self.card()
        DeviationReason.objects.filter(code='PAYMENT').update(is_active=False)
        self.client.force_login(self.member)
        response = self.client.get(board_url(self.board), {'card': card.pk, 'edit': 1})
        choices = response.context['form'].fields['due_reason'].widget.choices
        labels = [label for _, label in choices]
        self.assertEqual(labels[0], '— выберите причину —')
        self.assertNotIn('Ждём оплату', labels)
        self.assertEqual(labels[1:3], ['Ждём материал (снабжение)', 'Нет КД / ждём конструктора'])
        self.assertEqual(labels[-1], 'Другое')

    def test_the_create_form_asks_no_reason(self):
        self.client.force_login(self.member)
        response = self.client.get(board_url(self.board), {'new': column_of(self.board).pk})
        self.assertNotIn('due_reason', response.context['form'].fields)
        self.assertNotContains(response, 'Причина переноса')


# --------------------------------------------------------------------------
# A move of the срок
# --------------------------------------------------------------------------


class DueMoveTests(DueMixin, TestCase):
    def setUp(self):
        self.subject = self.card('Заказ 3-1579', assignees=[self.member, self.colleague], due_date=due(5))

    def test_a_new_card_keeps_its_first_srok(self):
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.original_due_date, due(5))
        self.move(self.subject, due(9))
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.original_due_date, due(5))
        child = create_subtask(self.subject, actor=self.member, title='Корпус', due_date=due(3))
        self.assertEqual(child.original_due_date, due(3))

    def test_without_a_reason_nothing_is_written(self):
        version = self.subject.version
        with self.assertRaisesMessage(DueReasonError, 'Срок переносится только с причиной'):
            self.move(self.subject, due(9), reason=None, comment='Ждём лист')
        self.subject.refresh_from_db()
        self.assertEqual(task_of(self.subject).due_date, due(5))
        self.assertEqual(self.subject.version, version)
        self.assertFalse(BoardCardDueChange.objects.exists())
        self.assertFalse(BoardCardEvent.objects.filter(card=self.subject, kind=BoardCardEvent.Kind.EDITED).exists())
        self.assertFalse(notes(Notification.EventType.BOARD_DUE_CHANGED).exists())

    def test_an_inactive_or_unknown_reason_is_refused(self):
        DeviationReason.objects.filter(code='OTHER').update(is_active=False)
        with self.assertRaisesMessage(DueReasonError, 'нет среди действующих'):
            self.move(self.subject, due(9), reason='OTHER')
        card = self.subject
        with self.assertRaisesMessage(DueReasonError, 'нет среди действующих'):
            update_card(
                card, actor=self.member, title=card.title, description='', due_date=due(9),
                assignee_ids=assignee_ids(card), due_reason_id=999999,
            )
        with self.assertRaisesMessage(DueReasonError, 'из списка'):
            update_card(
                card, actor=self.member, title=card.title, description='', due_date=due(9),
                assignee_ids=assignee_ids(card), due_reason_id='мусор',
            )
        self.assertFalse(BoardCardDueChange.objects.exists())

    def test_a_long_comment_is_refused(self):
        with self.assertRaisesMessage(DueReasonError, 'не длиннее 500'):
            self.move(self.subject, due(9), comment='я' * 501)
        self.assertFalse(BoardCardDueChange.objects.exists())

    def test_one_move_one_row_one_entry(self):
        self.move(self.subject, due(9), reason='DESIGN', comment='  Ждём КД от Петрова  ')
        change = BoardCardDueChange.objects.get()
        self.assertEqual(
            (change.card_id, change.old_due, change.new_due, change.reason.code, change.comment, change.changed_by),
            (self.subject.pk, due(5), due(9), 'DESIGN', 'Ждём КД от Петрова', self.member),
        )
        self.assertEqual(change.shift_days, 4)
        entry = BoardCardEvent.objects.get(card=self.subject, kind=BoardCardEvent.Kind.EDITED)
        self.assertEqual(entry.details['fields'], ['due_date'])
        self.assertEqual(entry.details['due_reason_id'], change.reason_id)
        # The journal names identifiers only, never the comment.
        self.assertNotIn('Петров', str(entry.details))
        self.subject.refresh_from_db()
        self.assertEqual(self.subject.version, 2)
        self.assertEqual(task_of(self.subject).due_date, due(9))

    def test_two_moves_two_rows_earlier_too(self):
        self.move(self.subject, due(9))
        self.move(self.subject, due(2), reason='CAPACITY')
        rows = list(BoardCardDueChange.objects.order_by('pk').values_list('old_due', 'new_due'))
        self.assertEqual(rows, [(due(5), due(9)), (due(9), due(2))])
        self.assertEqual(BoardCardDueChange.objects.order_by('pk').last().shift_days, -7)

    def test_an_edit_that_keeps_the_srok_writes_no_history_and_ignores_a_reason(self):
        card = self.subject
        update_card(
            card, actor=self.member, title='Новое название', description='', due_date=due(5),
            assignee_ids=assignee_ids(card), due_reason_id=reason_id(), due_comment='лишнее',
        )
        self.assertFalse(BoardCardDueChange.objects.exists())
        entry = BoardCardEvent.objects.get(card=card, kind=BoardCardEvent.Kind.EDITED)
        self.assertNotIn('due_reason_id', entry.details)
        self.assertFalse(notes(Notification.EventType.BOARD_DUE_CHANGED).exists())

    def test_a_stale_form_is_refused_before_the_reason(self):
        self.move(self.subject, due(9))
        with self.assertRaises(StaleCardError):
            self.move(self.subject, due(12), expected_version=1)
        self.assertEqual(BoardCardDueChange.objects.count(), 1)

    def test_a_first_srok_needs_no_reason(self):
        # A card's срок is required, so no card is without one today; the rule
        # for the first one is the service's: no reason asked, none stored.
        from ..services import _clean_due_reason

        self.assertIsNone(_clean_due_reason(None, needed=False, actor=self.member, board=self.board, card=self.subject))
        with self.assertRaises(DueReasonError):
            _clean_due_reason(None, needed=True, actor=self.member, board=self.board, card=self.subject)
        first = BoardCardDueChange.objects.create(
            card=self.subject, old_due=None, new_due=due(5), changed_by=self.member,
        )
        self.assertIsNone(first.reason)
        self.assertIsNone(first.shift_days)

    def test_a_card_without_a_srok_cannot_lose_it(self):
        # The срок of a card is required: removing it is refused before the
        # reason is even asked, and writes nothing.
        from ..services import BoardError

        with self.assertRaises(BoardError):
            self.move(self.subject, None)
        self.assertFalse(BoardCardDueChange.objects.exists())

    def test_the_audience_hears_of_it_but_not_the_editor(self):
        follower = self.admin
        toggle_card_subscription(self.subject, actor=follower, subscribe=True)
        author_card = self.card('Чужая', actor=self.owner, assignees=[self.colleague])
        self.move(self.subject, datetime.date(2026, 10, 30))
        got = notes(Notification.EventType.BOARD_DUE_CHANGED)
        # The author is the editor here; the other исполнитель and the follower.
        self.assertEqual(sorted(note.recipient_id for note in got), sorted([self.colleague.pk, follower.pk]))
        note = got.first()
        self.assertEqual(note.title, f'Срок карточки {self.subject.code} перенесён на 30.10.2026')
        self.assertEqual(note.source_type, Notification.SourceType.TASK)
        self.assertEqual(note.related_task_id, task_of(self.subject).pk)
        self.assertNotIn('материал', note.title + note.message)
        self.assertNotIn(Notification.EventType.BOARD_DUE_CHANGED, EMAIL_ELIGIBLE_EVENTS)
        self.assertFalse(note.deliveries.exists())
        # Another card's author hears of a move made by someone else.
        self.move(author_card, due(8), actor=self.colleague)
        self.assertTrue(notes(Notification.EventType.BOARD_DUE_CHANGED).filter(recipient=self.owner).exists())

    def test_a_subtask_moves_by_the_same_rule(self):
        child = create_subtask(self.subject, actor=self.member, title='Корпус', due_date=due(3))
        with self.assertRaises(DueReasonError):
            self.move(child, due(4), reason=None)
        self.move(child, due(4))
        self.assertEqual(BoardCardDueChange.objects.get().card_id, child.pk)


class DueMoveViewTests(DueMixin, TestCase):
    def setUp(self):
        self.subject = self.card('Заказ', due_date=due(5))
        self.client.force_login(self.member)

    def post(self, **overrides):
        data = {
            'title': self.subject.title,
            'description': '',
            'due_date': due(9).isoformat(),
            'assignees': [self.member.pk],
            'version': 1,
        }
        data.update(overrides)
        return self.client.post(reverse('boards:card_update', args=[self.board.pk, self.subject.pk]), data)

    def test_the_form_draws_the_reason_fields_with_the_stored_srok(self):
        response = self.client.get(board_url(self.board), {'card': self.subject.pk, 'edit': 1})
        self.assertContains(response, 'data-due-reason')
        self.assertContains(response, f'data-stored-due="{due(5).isoformat()}"')
        self.assertContains(response, 'Причина переноса')
        self.assertContains(response, 'Комментарий к переносу')
        self.assertContains(response, 'если меняете срок')

    def test_a_refusal_lands_on_the_reason_and_keeps_the_input(self):
        response = self.post(due_comment='Ждём лист')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Срок переносится только с причиной', str(response.context['form'].errors['due_reason']))
        self.assertContains(response, 'Ждём лист')
        self.assertContains(response, f'value="{due(9).isoformat()}"')
        self.assertEqual(task_of(self.subject).due_date, due(5))

    def test_success(self):
        response = self.post(due_reason=reason_id('EQUIPMENT'), due_comment='Станок')
        self.assertEqual(response.status_code, 302)
        change = BoardCardDueChange.objects.get()
        self.assertEqual((change.reason.code, change.comment), ('EQUIPMENT', 'Станок'))


# --------------------------------------------------------------------------
# What the board shows
# --------------------------------------------------------------------------


class DueDisplayTests(DueMixin, TestCase):
    def setUp(self):
        self.subject = self.card('Заказ', due_date=due(5))
        self.quiet = self.card('Тихая', due_date=due(6))

    def test_words(self):
        self.assertEqual(due_change_label(1), 'перенесён 1 раз')
        self.assertEqual(due_change_label(2), 'перенесён 2 раза')
        self.assertEqual(due_change_label(5), 'перенесён 5 раз')
        self.assertEqual(due_change_hint(2), 'Срок переносили 2 раза')

    def test_tile_and_panel(self):
        self.move(self.subject, due(9), comment='Лист с завода')
        self.move(self.subject, due(12), reason='DEFECT')
        state = build_board_state(self.board, self.main, self.member, card_id=self.subject.pk)
        tiles = {item['card'].pk: item for column in state['columns'] for item in column['cards']}
        self.assertEqual(tiles[self.subject.pk]['due_change_count'], 2)
        self.assertEqual(tiles[self.quiet.pk]['due_change_count'], 0)
        panel = state['card']
        self.assertEqual(panel['due_change_label'], 'перенесён 2 раза')
        self.assertEqual(panel['original_due_date'], due(5))
        # Oldest first: the table reads as the card's story.
        self.assertEqual([change.new_due for change in panel['due_changes']], [due(9), due(12)])

        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.subject.pk}).content.decode()
        self.assertIn('title="Срок переносили 2 раза" aria-label="Срок переносили 2 раза">↻2</span>', page)
        self.assertEqual(page.count('board-tile__due-moves'), 1)
        self.assertIn('перенесён 2 раза', page)
        self.assertIn(f'исходный {due(5):%d.%m.%Y}', page)
        self.assertIn('Брак', page)
        self.assertIn('Лист с завода', page)

    def test_an_old_card_reads_a_dash(self):
        self.subject.original_due_date = None
        self.subject.save(update_fields=['original_due_date'])
        self.move(self.subject, due(9))
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.subject.pk}).content.decode()
        self.assertNotIn('исходный', page)
        rows = {row['card'].pk: row for row in build_board_table(self.board, self.main, self.member)['rows']}
        self.assertIsNone(rows[self.subject.pk]['original_due_date'])
        table = self.client.get(board_url(self.board), {'view': 'table'}).content.decode()
        self.assertIn('—', table)

    def test_a_never_moved_card_shows_nothing(self):
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.quiet.pk}).content.decode()
        self.assertNotIn('board-tile__due-moves', page)
        self.assertNotIn('перенесён', page)
        self.assertNotIn('исходный', page)

    def test_table_columns_sort_and_excel(self):
        self.move(self.subject, due(9))
        self.move(self.subject, due(12), reason='DEFECT')
        state = build_board_table(self.board, self.main, self.member, sort='-changes')
        rows = state['rows']
        self.assertEqual([row['card'].pk for row in rows], [self.subject.pk, self.quiet.pk])
        self.assertEqual(
            (rows[0]['original_due_date'], rows[0]['due_change_count'], rows[0]['last_due_reason']),
            (due(5), 2, 'Брак'),
        )
        self.assertEqual((rows[1]['due_change_count'], rows[1]['last_due_reason']), (0, ''))
        ascending = build_board_table(self.board, self.main, self.member, sort='changes')['rows']
        self.assertEqual([row['card'].pk for row in ascending], [self.quiet.pk, self.subject.pk])

        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'view': 'table'}).content.decode()
        for header in ('Исходный срок', 'Переносов', 'Последняя причина'):
            self.assertIn(header, page)
        content = self.client.get(board_url(self.board), {'view': 'table', 'export': 'xlsx'}).content
        sheet, _ = read_xlsx(content)
        header = values(sheet)[0]
        at = header.index('Исходный срок')
        self.assertEqual(header[at:at + 3], ['Исходный срок', 'Переносов', 'Последняя причина'])
        by_code = {row[0][1]: row for row in sheet[1:]}
        moved = by_code[self.subject.code]
        self.assertEqual(moved[at:at + 3], [('d', due(5)), ('n', Decimal(2)), ('s', 'Брак')])
        quiet = by_code[self.quiet.code]
        self.assertEqual(quiet[at + 1:at + 3], [('n', Decimal(0)), ('', None)])

    def test_the_tile_count_costs_no_query(self):
        self.client.force_login(self.member)
        url = board_url(self.board)
        with CaptureQueriesContext(connection) as before:
            self.client.get(url)
        for days in (7, 8, 9):
            self.move(self.subject, due(days))
            self.move(self.quiet, due(days + 1))
        with CaptureQueriesContext(connection) as after:
            self.client.get(url)
        self.assertEqual(len(after), len(before))


# --------------------------------------------------------------------------
# The reminders
# --------------------------------------------------------------------------


class DueRemindersTests(DueMixin, TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.tomorrow = add_working_days(self.today, 1)
        self.soon = self.card('Скоро', assignees=[self.member, self.colleague], actor=self.owner)
        self.set_due(self.soon, self.tomorrow)
        self.late = self.card('Поздно', assignees=[self.colleague], actor=self.member)
        self.set_due(self.late, self.today - datetime.timedelta(days=2))
        self.far = self.card('Далеко', due_date=add_working_days(self.today, 3))

    def test_due_soon_to_the_assignees_overdue_to_the_audience(self):
        toggle_card_subscription(self.soon, actor=self.admin, subscribe=True)
        toggle_card_subscription(self.late, actor=self.admin, subscribe=True)
        self.assertEqual(send_due_reminders(), {'due_soon': 2, 'overdue': 3})
        soon = notes(Notification.EventType.BOARD_DUE_SOON)
        # Only the исполнители — not the author, not the follower.
        self.assertEqual(sorted(n.recipient_id for n in soon), sorted([self.member.pk, self.colleague.pk]))
        self.assertTrue(all(n.related_task_id == task_of(self.soon).pk for n in soon))
        when = due_day_words(self.tomorrow).capitalize()
        self.assertEqual(soon.first().title, f'{when} срок карточки {self.soon.code}')
        late = notes(Notification.EventType.BOARD_OVERDUE)
        self.assertEqual(
            sorted(n.recipient_id for n in late), sorted([self.colleague.pk, self.member.pk, self.admin.pk]),
        )
        self.assertEqual(late.first().title, f'Карточка {self.late.code} просрочена')
        self.assertTrue(all(n.actor_id is None for n in [*soon, *late]))

    def test_both_are_mailed(self):
        self.assertIn(Notification.EventType.BOARD_DUE_SOON, EMAIL_ELIGIBLE_EVENTS)
        self.assertIn(Notification.EventType.BOARD_OVERDUE, EMAIL_ELIGIBLE_EVENTS)
        send_due_reminders()
        for note in Notification.objects.filter(
            event_type__in=[Notification.EventType.BOARD_DUE_SOON, Notification.EventType.BOARD_OVERDUE],
        ):
            self.assertEqual(note.deliveries.count(), 1)

    def test_a_second_run_creates_nothing_a_moved_srok_asks_again(self):
        send_due_reminders()
        self.assertEqual(send_due_reminders(), {'due_soon': 0, 'overdue': 0})
        self.set_due(self.late, self.today - datetime.timedelta(days=1))
        self.assertEqual(send_due_reminders()['overdue'], 2)
        self.assertEqual(notes(Notification.EventType.BOARD_OVERDUE).count(), 4)

    def test_a_real_move_asks_again(self):
        send_due_reminders()
        self.move(self.far, self.tomorrow, actor=self.member)
        self.assertEqual(send_due_reminders()['due_soon'], 1)
        self.assertTrue(notes(Notification.EventType.BOARD_DUE_SOON).filter(related_task=task_of(self.far)).exists())

    def test_due_today(self):
        self.set_due(self.soon, self.today)
        send_due_reminders()
        self.assertEqual(
            notes(Notification.EventType.BOARD_DUE_SOON).first().title, f'Сегодня срок карточки {self.soon.code}',
        )

    def test_a_friday_reaches_monday(self):
        friday = datetime.date(2026, 10, 9)
        monday = datetime.date(2026, 10, 12)
        self.assertEqual(add_working_days(friday, 1), monday)
        self.set_due(self.soon, monday)
        self.set_due(self.far, datetime.date(2026, 10, 13))
        self.set_due(self.late, friday)
        # Monday (two исполнители) and today, Friday (one); not Tuesday.
        self.assertEqual(send_due_reminders(today=friday), {'due_soon': 3, 'overdue': 0})
        self.assertEqual(due_day_words(monday, today=friday), 'в понедельник')
        self.assertEqual(due_day_words(friday, today=friday), 'сегодня')
        self.assertEqual(due_day_words(datetime.date(2026, 10, 8), today=datetime.date(2026, 10, 7)), 'завтра')

    def test_closed_tasks_and_archived_boards_are_skipped(self):
        complete_card(self.soon, actor=self.member, execution_comment='Готово')
        Board.objects.filter(pk=self.board.pk).update(status=Board.Status.ARCHIVED)
        self.assertEqual(send_due_reminders(), {'due_soon': 0, 'overdue': 0})
        Board.objects.filter(pk=self.board.pk).update(status=Board.Status.ACTIVE)
        self.assertEqual(send_due_reminders(), {'due_soon': 0, 'overdue': 2})

    def test_people_who_no_longer_read_the_board_are_skipped(self):
        # A follower of the late card who is no member: a reader only while
        # board access is widened (`WidenedBoardAccess`), none under the real
        # rule — and then not told.
        toggle_card_subscription(self.late, actor=self.outsider, subscribe=True)
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset()):
            send_due_reminders()
        recipients = {
            note.recipient_id
            for note in notes(Notification.EventType.BOARD_OVERDUE).filter(related_task=task_of(self.late))
        }
        self.assertEqual(recipients, {self.colleague.pk, self.member.pk})

    def test_subtasks_are_reminded_too(self):
        child = create_subtask(self.far, actor=self.member, title='Корпус', assignees=[self.colleague])
        self.set_due(child, self.today - datetime.timedelta(days=1))
        send_due_reminders()
        self.assertTrue(notes(Notification.EventType.BOARD_OVERDUE).filter(
            related_task=task_of(child), recipient=self.colleague,
        ).exists())

    def test_the_command(self):
        out = StringIO()
        with self.assertLogs('ecosystem.workflow', level='INFO') as logs:
            call_command('board_due_reminders', stdout=out)
        self.assertIn('2', out.getvalue())
        lines = [line for line in logs.output if 'board.due_reminders' in line]
        self.assertEqual(len(lines), 1)
        call_command('board_due_reminders', stdout=StringIO())
        self.assertEqual(notes(Notification.EventType.BOARD_DUE_SOON).count(), 2)


# --------------------------------------------------------------------------
# «Отклонения»
# --------------------------------------------------------------------------


class DeviationReportTests(DueMixin, TestCase):
    def setUp(self):
        self.first = self.card('Первая', due_date=due(5))
        self.second = self.card('Вторая', due_date=due(5))
        self.workshop = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        self.third = new_card(
            self.board, self.member, 'Третья', assignees=[self.member], sub_board=self.workshop,
            column=column_of(self.board, 'TODO', self.workshop), due_date=due(5),
        )
        self.move(self.first, due(8), comment='Лист')  # MATERIAL +3
        self.move(self.first, due(10), reason='DEFECT')  # DEFECT +2
        self.move(self.second, due(9))  # MATERIAL +4
        self.move(self.third, due(6), reason='DEFECT', actor=self.colleague)  # DEFECT +1
        self.today = timezone.localdate()

    def url(self, **params):
        return reverse('boards:deviations', args=[self.board.pk]) + (
            '?' + '&'.join(f'{k}={v}' for k, v in params.items()) if params else ''
        )

    def test_summary_and_list(self):
        report = build_deviation_report(
            self.board, date_from=self.today - datetime.timedelta(days=29), date_to=self.today,
        )
        summary = [(entry['reason'].code, entry['moves'], entry['shift'], entry['cards']) for entry in report['summary']]
        self.assertEqual(summary, [('MATERIAL', 2, 7, 2), ('DEFECT', 2, 3, 2)])
        self.assertEqual((report['moves'], report['cards'], report['shift']), (4, 3, 10))
        self.assertEqual([row['card'].pk for row in report['rows']], [self.third.pk, self.second.pk, self.first.pk, self.first.pk])
        last = report['rows'][-1]
        self.assertEqual((last['old_due'], last['new_due'], last['shift'], last['comment'], last['who']),
                         (due(5), due(8), 3, 'Лист', self.member))

    def test_the_sub_board_and_the_period(self):
        report = build_deviation_report(self.board, date_from=self.today, date_to=self.today, sub_board=self.workshop)
        self.assertEqual([row['card'].pk for row in report['rows']], [self.third.pk])
        BoardCardDueChange.objects.filter(card=self.second).update(
            changed_at=timezone.now() - datetime.timedelta(days=40),
        )
        report = build_deviation_report(
            self.board, date_from=self.today - datetime.timedelta(days=29), date_to=self.today,
        )
        self.assertNotIn(self.second.pk, [row['card'].pk for row in report['rows']])
        self.assertEqual(report['moves'], 3)

    def test_a_first_srok_without_a_reason_is_no_deviation(self):
        BoardCardDueChange.objects.create(card=self.second, old_due=None, new_due=due(5), changed_by=self.member)
        report = build_deviation_report(self.board, date_from=self.today, date_to=self.today)
        self.assertEqual(report['moves'], 4)

    def test_the_page_for_a_reader_and_403_for_an_outsider(self):
        self.client.force_login(self.colleague)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['date_to'], self.today)
        self.assertEqual(response.context['date_from'], self.today - datetime.timedelta(days=29))
        page = response.content.decode()
        self.assertIn('Ждём материал (снабжение)', page)
        self.assertIn(self.first.code, page)
        self.assertIn('<span class="user-text">Лист</span>', page)
        response = self.client.get(self.url(sub=self.workshop.pk, **{'from': 'мусор'}))
        self.assertEqual([row['card'].pk for row in response.context['rows']], [self.third.pk])
        # The real board access: an outsider is no member.
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset()):
            self.client.force_login(self.outsider)
            self.assertEqual(self.client.get(self.url()).status_code, 403)
            self.client.force_login(self.colleague)
            self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_every_reader_reaches_it_from_the_menu(self):
        self.client.force_login(self.colleague)
        page = self.client.get(board_url(self.board)).content.decode()
        self.assertIn(reverse('boards:deviations', args=[self.board.pk]), page)
        self.assertIn('Отклонения', page)

    def test_excel(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url(export='xlsx'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('otkloneniya', response['Content-Disposition'])
        rows, styles = read_xlsx(response.content)
        self.assertEqual(values(rows)[0], [
            'Дата', 'Карточка', 'Название', 'Поддоска', 'Было', 'Стало', 'Сдвиг, дн.', 'Причина', 'Комментарий', 'Кто',
        ])
        self.assertIn('formatCode="dd.mm.yyyy"', styles)
        last = rows[-1]
        self.assertEqual(last[1], ('s', self.first.code))
        self.assertEqual(last[4:8], [('d', due(5)), ('d', due(8)), ('n', Decimal(3)), ('s', 'Ждём материал (снабжение)')])
        self.assertEqual(rows[1][3], ('s', 'Цех ПиР'))
        self.assertEqual(rows[1][8], ('', None))

    def test_the_query_count_is_constant(self):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as before:
            self.client.get(self.url())
        for days in (11, 12, 13, 14):
            self.move(self.second, due(days), reason='CAPACITY')
        extra = self.card('Ещё', due_date=due(5))
        self.move(extra, due(7), reason='OTHER')
        with CaptureQueriesContext(connection) as after:
            response = self.client.get(self.url())
        self.assertEqual(len(response.context['rows']), 9)
        self.assertEqual(len(after), len(before))


# --------------------------------------------------------------------------
# Stage 20 follow-up: a subtask's completion
# --------------------------------------------------------------------------


class SubtaskCompletedAudienceTests(DueMixin, TestCase):
    def test_a_subtask_tells_its_followers_not_its_author(self):
        parent = self.card('Заказ', assignees=[self.member])
        child = create_subtask(parent, actor=self.member, title='Корпус', assignees=[self.colleague])
        toggle_card_subscription(child, actor=self.admin, subscribe=True)
        complete_card(child, actor=self.colleague, execution_comment='Готово')
        got = notes(Notification.EventType.BOARD_CARD_COMPLETED).filter(related_task=task_of(child))
        self.assertEqual([note.recipient_id for note in got], [self.admin.pk])

    def test_a_card_still_tells_its_author(self):
        card = self.card('Заказ', assignees=[self.colleague], actor=self.member)
        complete_card(card, actor=self.colleague, execution_comment='Готово')
        got = notes(Notification.EventType.BOARD_CARD_COMPLETED).filter(related_task=task_of(card))
        self.assertEqual([note.recipient_id for note in got], [self.member.pk])
