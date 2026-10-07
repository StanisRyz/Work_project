"""«Приём заявок» (stage 24): the board's intake, `BoardRequest`, «Заявки»,
«Входящие», the `REQUEST` notification source and `request_changed`.

Most tests run with full board access widened (`BoardFixtureMixin`); those
about who sees what restore the real constant, so the outsider really is a
stranger to the board.
"""

import datetime
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from ecosystem.workdays import add_working_days
from notifications.models import Notification, NotificationDelivery
from notifications.services import (
    describe_notification_source,
    get_notification_url,
    get_required_action,
)
from realtime.events import BOARD_CHANGE_REQUEST_CHANGED, BOARD_CHANGES

from ..models import BoardCardComment, BoardCardEvent, BoardCardFieldValue, BoardIntakeHandler, BoardRequest
from ..permissions import can_use_requests
from ..selectors import build_inbox, build_my_requests, describe_card_event
from ..services import (
    BoardError,
    FieldValueError,
    accept_request,
    archive_board,
    complete_card,
    create_field,
    mark_duplicate,
    reject_request,
    remove_board_member,
    submit_request,
    update_intake,
    withdraw_request,
)
from .helpers import BoardFixtureMixin, board_url, fragment_url, fresh_code, main_sub_board, make_user
from .test_journal import task_of

REAL_ACCESS = mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN}))


class IntakeFixture(BoardFixtureMixin):
    def setUp(self):
        self.requester = make_user('shop_master', UserProfile.Role.MAS)
        self.number = create_field(self.board, actor=self.owner, name='Номер заявки', kind='TEXT')
        self.priority = create_field(
            self.board, actor=self.owner, name='Приоритет', kind='SELECT',
            options=[('Высокий', 'red'), ('Низкий', 'gray')],
        )
        self.quantity = create_field(self.board, actor=self.owner, name='Кол-во', kind='NUMBER')
        self.secret = create_field(self.board, actor=self.owner, name='Себестоимость', kind='NUMBER')
        self.high = self.priority.options.get(label='Высокий')

    def enable(self, **extra):
        settings = {
            'enabled': True,
            'due_days': 2,
            'handler_ids': [self.member.pk],
            'form_fields': {
                self.number.pk: (True, True),
                self.priority.pk: (True, False),
                self.quantity.pk: (True, False),
            },
            **extra,
        }
        update_intake(self.board, actor=self.owner, **settings)
        self.board.refresh_from_db()

    def submit(self, title='Нарезать заготовки', *, author=None, **extra):
        values = extra.pop('field_values', {self.number.pk: '3-1579', self.priority.pk: self.high.pk})
        return submit_request(
            self.board, author=author or self.requester, title=title,
            description=extra.pop('description', 'Партия 40 шт. для цеха МП'),
            field_values=values, **extra,
        )


# --------------------------------------------------------------------------
# The intake settings
# --------------------------------------------------------------------------


class IntakeSettingsTests(IntakeFixture, TestCase):
    def test_the_manager_sets_it_up_and_the_same_again_changes_nothing(self):
        work = self.column('IN_PROGRESS')
        with mock.patch('boards.services.emit_board_updated') as emitted:
            self.assertTrue(update_intake(
                self.board, actor=self.owner, enabled=True, column=work.pk, due_days=3,
                hint='Пишите номер ЗНП', handler_ids=[self.member.pk],
                form_fields={self.number.pk: (True, True), self.priority.pk: (True, False)},
            ))
            self.assertFalse(update_intake(
                self.board, actor=self.owner, enabled=True, column=work.pk, due_days=3,
                hint='Пишите номер ЗНП', handler_ids=[self.member.pk],
                form_fields={self.number.pk: (True, True), self.priority.pk: (True, False)},
            ))
        self.assertEqual(emitted.call_count, 1)
        self.assertEqual(emitted.call_args.args[1], 'structure_changed')
        self.board.refresh_from_db()
        self.assertTrue(self.board.intake_enabled)
        self.assertEqual((self.board.intake_column_id, self.board.intake_sub_board_id), (work.pk, self.main.pk))
        self.assertEqual(self.board.intake_due_days, 3)
        self.assertEqual(list(self.board.intake_handlers.values_list('user_id', flat=True)), [self.member.pk])
        self.number.refresh_from_db()
        self.quantity.refresh_from_db()
        self.assertEqual((self.number.in_request_form, self.number.required_in_request), (True, True))
        self.assertEqual((self.quantity.in_request_form, self.quantity.required_in_request), (False, False))

    def test_rights_and_refusals(self):
        for actor in (self.member, self.outsider):
            with self.subTest(actor=actor.username), self.assertRaisesMessage(BoardError, 'настраивает владелец'):
                update_intake(self.board, actor=actor, enabled=True)
        with self.assertRaisesMessage(BoardError, 'Разбирающими'):
            update_intake(self.board, actor=self.owner, enabled=True, handler_ids=[self.outsider.pk])
        with self.assertRaisesMessage(BoardError, 'Колонка не найдена'):
            update_intake(self.board, actor=self.owner, enabled=True, column=999999)
        done = self.main.columns.get(is_done=True)
        with self.assertRaisesMessage(BoardError, 'когда её задача выполнена'):
            update_intake(self.board, actor=self.owner, enabled=True, column=done.pk)
        with self.assertRaisesMessage(BoardError, 'от 0 до 365'):
            update_intake(self.board, actor=self.owner, enabled=True, due_days=400)

    def test_a_required_field_is_in_the_form_and_the_database_says_so(self):
        update_intake(self.board, actor=self.owner, enabled=True, form_fields={self.number.pk: (False, True)})
        self.number.refresh_from_db()
        self.assertTrue(self.number.in_request_form and self.number.required_in_request)
        with self.assertRaises(IntegrityError), transaction.atomic():
            type(self.number).objects.filter(pk=self.number.pk).update(in_request_form=False)

    def test_the_page_asks_the_right_before_the_method(self):
        url = reverse('boards:intake', args=[self.board.pk])
        self.client.force_login(self.member)
        for method in (self.client.get, self.client.post):
            self.assertEqual(method(url).status_code, 403)
        self.client.force_login(self.owner)
        page = self.client.get(url).content.decode()
        for needle in ('Принимать заявки', 'Кто разбирает заявки', 'Номер заявки', 'обязательно'):
            self.assertIn(needle, page)
        response = self.client.post(url, {
            'enabled': 'on', 'column': '', 'due_days': '2', 'hint': 'Подсказка',
            'handlers': [self.member.pk],
            f'form_{self.number.pk}': 'required', f'form_{self.priority.pk}': 'on',
        })
        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.board.refresh_from_db()
        self.assertEqual((self.board.intake_enabled, self.board.intake_due_days), (True, 2))
        # The board's «⋯» links there for the manager.
        self.assertIn(url, self.client.get(board_url(self.board)).content.decode())

    def test_removing_a_handler_from_the_board_drops_them(self):
        self.enable()
        remove_board_member(self.board, self.member, actor=self.owner)
        self.assertFalse(BoardIntakeHandler.objects.filter(board=self.board).exists())


# --------------------------------------------------------------------------
# Filing a request
# --------------------------------------------------------------------------


class SubmitTests(IntakeFixture, TestCase):
    def test_a_stranger_to_the_board_files_one_with_parsed_values(self):
        self.enable()
        board_request = self.submit(field_values={
            self.number.pk: '  3-1579 ', self.priority.pk: str(self.high.pk), self.quantity.pk: '1 250,50',
            self.secret.pk: '999',  # not in the form: ignored
        }, desired_date=datetime.date(2026, 11, 2))
        self.assertEqual(board_request.status, BoardRequest.Status.NEW)
        self.assertEqual(board_request.field_values, {
            str(self.number.pk): '3-1579', str(self.priority.pk): self.high.pk, str(self.quantity.pk): '1250.5',
        })
        self.assertEqual(board_request.desired_date, datetime.date(2026, 11, 2))

    def test_required_fields_and_the_card_parse(self):
        self.enable()
        with self.assertRaisesMessage(FieldValueError, '«Номер заявки»: обязательное поле заявки.'):
            self.submit(field_values={self.priority.pk: self.high.pk})
        with self.assertRaisesMessage(FieldValueError, '«Кол-во»: введите число'):
            self.submit(field_values={self.number.pk: '1', self.quantity.pk: 'много'})
        with self.assertRaisesMessage(BoardError, 'название заявки'):
            self.submit(title='  ')
        self.assertFalse(BoardRequest.objects.exists())

    def test_intake_off_and_an_archived_board_take_nothing(self):
        with self.assertRaisesMessage(BoardError, 'не принимает заявки'):
            self.submit()
        self.enable()
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            self.submit()

    def test_an_inactive_employee_files_nothing(self):
        self.enable()
        self.requester.userprofile.is_active = False
        self.requester.userprofile.save()
        with self.assertRaisesMessage(BoardError, 'активный сотрудник'):
            self.submit()

    def test_withdraw_is_the_authors_while_new(self):
        self.enable()
        board_request = self.submit()
        with self.assertRaisesMessage(BoardError, 'только её автор'):
            withdraw_request(board_request, actor=self.member)
        withdraw_request(board_request, actor=self.requester)
        board_request.refresh_from_db()
        self.assertEqual(
            (board_request.status, board_request.decided_by), (BoardRequest.Status.WITHDRAWN, self.requester),
        )
        with self.assertRaisesMessage(BoardError, 'Автор отозвал заявку.'):
            withdraw_request(board_request, actor=self.requester)
        with self.assertRaisesMessage(BoardError, 'Автор отозвал заявку.'):
            reject_request(board_request, actor=self.member, reason='Поздно')

    def test_the_form_page(self):
        self.enable(hint='Укажите номер ЗНП')
        url = reverse('boards:request_create', args=[self.board.pk])
        self.client.force_login(self.requester)
        page = self.client.get(url).content.decode()
        for needle in ('Укажите номер ЗНП', 'Номер заявки *', 'Приоритет', '● Высокий', 'Кол-во'):
            self.assertIn(needle, page)
        self.assertNotIn('Себестоимость', page)
        refused = self.client.post(url, {'title': 'Сделать', f'field_{self.priority.pk}': self.high.pk})
        self.assertEqual(refused.status_code, 400)
        self.assertContains(refused, 'обязательное поле заявки', status_code=400)
        self.assertContains(refused, 'value="Сделать"', status_code=400)
        response = self.client.post(url, {
            'title': 'Сделать', f'field_{self.number.pk}': '7', 'description': 'Срочно',
        })
        board_request = BoardRequest.objects.get()
        self.assertRedirects(
            response, reverse('boards:request_detail', args=[board_request.pk]), fetch_redirect_response=False,
        )
        # A board that takes no requests is a 404, like one that does not exist.
        update_intake(self.board, actor=self.owner, enabled=False, handler_ids=[self.member.pk])
        self.assertEqual(self.client.get(url).status_code, 404)


# --------------------------------------------------------------------------
# Sorting: accept, reject, duplicate
# --------------------------------------------------------------------------


class DecideTests(IntakeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.enable(column=self.column('IN_PROGRESS').pk, due_days=3)

    def test_accept_makes_the_card_with_its_fields_message_and_journal(self):
        board_request = self.submit(field_values={self.number.pk: '3-1579', self.priority.pk: self.high.pk})
        card = accept_request(board_request, actor=self.member, assignee_ids=[self.colleague.pk])
        board_request.refresh_from_db()
        self.assertEqual((board_request.status, board_request.card), (BoardRequest.Status.ACCEPTED, card))
        self.assertEqual(card.column, self.column('IN_PROGRESS'))
        self.assertEqual(card.title, 'Нарезать заготовки')
        self.assertEqual(card.description, 'Партия 40 шт. для цеха МП')
        task = task_of(card)
        # No desired date: `intake_due_days` working days from today.
        self.assertEqual(task.due_date, add_working_days(timezone.localdate(), 3))
        self.assertEqual(list(task.assignees.values_list('user_id', flat=True)), [self.colleague.pk])
        values = {row.field_id: row for row in BoardCardFieldValue.objects.filter(card=card)}
        self.assertEqual(values[self.number.pk].value_text, '3-1579')
        self.assertEqual(values[self.priority.pk].option_id, self.high.pk)
        message = BoardCardComment.objects.get(card=card)
        self.assertEqual((message.author, message.text), (
            self.member, 'Заявка от shop_master: Партия 40 шт. для цеха МП',
        ))
        created = card.events.get(kind=BoardCardEvent.Kind.CREATED)
        self.assertEqual(created.details['request_id'], board_request.pk)
        self.assertIn(f'из заявки №{board_request.pk}', describe_card_event(created, card.code))

    def test_accept_takes_the_desired_date_and_corrections(self):
        board_request = self.submit(desired_date=datetime.date(2026, 12, 1))
        card = accept_request(
            board_request, actor=self.member, assignee_ids=[self.member.pk],
            column=self.column('TODO').pk, title='Нарезать 40 заготовок', description='Уточнено',
        )
        self.assertEqual(task_of(card).due_date, datetime.date(2026, 12, 1))
        self.assertEqual((card.title, card.description, card.column), (
            'Нарезать 40 заготовок', 'Уточнено', self.column('TODO'),
        ))
        # The request's own words stay in the chat.
        self.assertIn('Партия 40 шт.', BoardCardComment.objects.get(card=card).text)

    def test_reject_needs_a_reason_and_a_repeat_is_refused(self):
        board_request = self.submit()
        with self.assertRaisesMessage(BoardError, 'причину отказа'):
            reject_request(board_request, actor=self.member, reason='   ')
        reject_request(board_request, actor=self.member, reason='Не наш профиль')
        board_request.refresh_from_db()
        self.assertEqual(
            (board_request.status, board_request.decision_comment),
            (BoardRequest.Status.REJECTED, 'Не наш профиль'),
        )
        for decide in (
            lambda: reject_request(board_request, actor=self.member, reason='Ещё раз'),
            lambda: accept_request(board_request, actor=self.member, assignee_ids=[self.member.pk]),
            lambda: mark_duplicate(board_request, actor=self.member, card_code='X-1'),
        ):
            with self.subTest(), self.assertRaisesMessage(BoardError, 'Заявку уже разобрали.'):
                decide()

    def test_duplicate_by_code_of_this_board_only(self):
        existing = self.card('Уже в работе')
        board_request = self.submit()
        other = make_user('other_owner', UserProfile.Role.PDO)
        from ..services import create_board, create_card

        foreign_board = create_board(name='Чужая', code=fresh_code(), owner=other, actor=other)
        foreign = create_card(
            main_sub_board(foreign_board), actor=other, title='Чужая', due_date=timezone.localdate(),
            assignee_ids=[other.pk],
        )
        with self.assertRaisesMessage(BoardError, 'не найдена на этой доске'):
            mark_duplicate(board_request, actor=self.member, card_code=foreign.code)
        with self.assertRaisesMessage(BoardError, 'не найдена на этой доске'):
            mark_duplicate(board_request, actor=self.member, card_code='чепуха')
        mark_duplicate(board_request, actor=self.member, card_code=existing.code.lower())
        board_request.refresh_from_db()
        self.assertEqual(
            (board_request.status, board_request.duplicate_of), (BoardRequest.Status.DUPLICATE, existing),
        )

    def test_rights_and_the_archive(self):
        board_request = self.submit()
        for actor in (self.requester,):
            with self.assertRaisesMessage(BoardError, 'участники доски'):
                reject_request(board_request, actor=actor, reason='Нет')
        with REAL_ACCESS, self.assertRaisesMessage(BoardError, 'участники доски'):
            reject_request(board_request, actor=self.outsider, reason='Нет')
        # A board with a new request is not archived; once sorted it is, and
        # an archived board decides nothing.
        with self.assertRaisesMessage(BoardError, 'разберите новые заявки'):
            archive_board(self.board, actor=self.owner)
        other = self.submit('Вторая')
        reject_request(board_request, actor=self.member, reason='Нет')
        withdraw_request(other, actor=self.requester)
        archive_board(self.board, actor=self.owner)
        third = BoardRequest.objects.create(board=self.board, author=self.requester, title='Старая')
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            reject_request(third, actor=self.member, reason='Нет')

    def test_the_database_keeps_the_shapes(self):
        board_request = self.submit()
        now = timezone.now()
        for status, extra in (
            ('ACCEPTED', {}),
            ('DUPLICATE', {}),
            ('REJECTED', {'decision_comment': ''}),
            ('NEW', {'decided_by': self.member, 'decided_at': now}),
            ('BOGUS', {}),
        ):
            values = {'status': status, 'decided_by': self.member, 'decided_at': now,
                      'decision_comment': 'x', **extra}
            if status == 'NEW':
                values['decision_comment'] = ''
            with self.subTest(status=status), self.assertRaises(IntegrityError), transaction.atomic():
                BoardRequest.objects.filter(pk=board_request.pk).update(**values)

    def test_the_inbox_routes(self):
        board_request = self.submit()
        accept_url = reverse('boards:request_accept', args=[self.board.pk, board_request.pk])
        reject_url = reverse('boards:request_reject', args=[self.board.pk, board_request.pk])
        with REAL_ACCESS:
            self.client.force_login(self.outsider)
            for url in (reverse('boards:inbox', args=[self.board.pk]), accept_url, reject_url):
                self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.member)
        inbox = self.client.get(reverse('boards:inbox', args=[self.board.pk])).content.decode()
        self.assertIn('Нарезать заготовки', inbox)
        self.assertIn('shop_master', inbox)
        page = self.client.get(reverse('boards:inbox_request', args=[self.board.pk, board_request.pk]))
        self.assertContains(page, 'Принять в работу')
        self.assertContains(page, 'Отметить дублем')
        self.client.get(accept_url)  # a GET changes nothing
        board_request.refresh_from_db()
        self.assertTrue(board_request.is_new)
        refused = self.client.post(reject_url, {'comment': ''})
        self.assertContains(refused, 'причину отказа', status_code=400)
        response = self.client.post(accept_url, {
            'accept-column': self.column('IN_PROGRESS').pk, 'accept-assignees': [self.member.pk],
            'accept-due_date': '2026-11-20', 'accept-title': 'Нарезать', 'accept-description': '',
        })
        board_request.refresh_from_db()
        self.assertEqual(board_request.status, BoardRequest.Status.ACCEPTED)
        self.assertRedirects(
            response,
            f"{reverse('boards:sub_board', args=[self.board.pk, self.main.pk])}?card={board_request.card_id}&tab=chat",
            fetch_redirect_response=False,
        )


# --------------------------------------------------------------------------
# Who sees what
# --------------------------------------------------------------------------


class VisibilityTests(IntakeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.enable()
        self.board_request = self.submit()

    def test_the_author_reads_a_projection_but_not_the_board(self):
        card = accept_request(self.board_request, actor=self.member, assignee_ids=[self.member.pk])
        with REAL_ACCESS:
            self.client.force_login(self.requester)
            page = self.client.get(reverse('boards:request_detail', args=[self.board_request.pk]))
            self.assertContains(page, card.code)
            self.assertContains(page, 'Сделать')          # the column it stands in
            self.assertContains(page, 'В работе')         # its task's state
            self.assertNotContains(page, f'?card={card.pk}')
            home = self.client.get(reverse('boards:requests')).content.decode()
            self.assertIn(card.code, home)
            self.assertNotIn(f'?card={card.pk}', home)
            self.assertEqual(self.client.get(board_url(self.board)).status_code, 403)
            self.assertEqual(
                self.client.get(reverse('boards:inbox', args=[self.board.pk])).status_code, 403,
            )
            complete_card(card, actor=self.member, execution_comment='Готово')
            page = self.client.get(reverse('boards:request_detail', args=[self.board_request.pk]))
            self.assertContains(page, 'Выполнена')
            self.assertContains(page, 'Готово')           # the closing column's name

    def test_a_stranger_gets_a_404_and_a_handler_goes_to_the_inbox(self):
        url = reverse('boards:request_detail', args=[self.board_request.pk])
        with REAL_ACCESS:
            self.client.force_login(self.outsider)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(
                self.client.post(reverse('boards:request_withdraw', args=[self.board_request.pk])).status_code,
                404,
            )
        self.client.force_login(self.member)
        self.assertRedirects(
            self.client.get(url),
            reverse('boards:inbox_request', args=[self.board.pk, self.board_request.pk]),
        )

    def test_the_menu_and_the_dashboard_card_follow_the_rule(self):
        self.client.force_login(self.outsider)
        self.assertIn(reverse('boards:requests'), self.client.get(reverse('dashboard:home')).content.decode())
        self.assertTrue(can_use_requests(self.outsider))
        update_intake(self.board, actor=self.owner, enabled=False, handler_ids=[self.member.pk])
        self.assertFalse(can_use_requests(self.outsider))
        page = self.client.get(reverse('dashboard:home')).content.decode()
        self.assertNotIn(reverse('boards:requests'), page)
        # The page itself answers every signed-in employee.
        self.assertEqual(self.client.get(reverse('boards:requests')).status_code, 200)

    def test_query_counts_do_not_grow(self):
        def count(url, user):
            self.client.force_login(user)
            with CaptureQueriesContext(connection) as queries:
                self.assertEqual(self.client.get(url).status_code, 200)
            return len(queries)

        home = reverse('boards:requests')
        inbox = reverse('boards:inbox', args=[self.board.pk])
        # One accepted request first: with a card to project the page reads
        # its board, task and columns — three queries, whatever comes after.
        accept_request(self.board_request, actor=self.member, assignee_ids=[self.member.pk])
        before = (count(home, self.requester), count(inbox, self.member))
        for index in range(6):
            board_request = self.submit(f'Заявка {index}')
            if index % 3 == 0:
                accept_request(board_request, actor=self.member, assignee_ids=[self.member.pk])
            elif index % 3 == 1:
                reject_request(board_request, actor=self.member, reason='Нет')
        self.card('Дубликат')
        after = (count(home, self.requester), count(inbox, self.member))
        self.assertEqual(before, after)


# --------------------------------------------------------------------------
# Notifications: the `REQUEST` source
# --------------------------------------------------------------------------


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class NotificationTests(IntakeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.requester.email = 'master@example.com'
        self.requester.save()
        self.member.email = 'member@example.com'
        self.member.save()
        self.enable()

    def events(self, event_type):
        return Notification.objects.filter(event_type=event_type)

    def test_new_goes_to_the_handlers_else_the_owner(self):
        board_request = self.submit()
        note = self.events('BOARD_REQUEST_NEW').get()
        self.assertEqual((note.recipient, note.source_type, note.related_board_request), (
            self.member, 'REQUEST', board_request,
        ))
        self.assertEqual(note.title, f'Новая заявка на доске «{self.board.name}»: Заявка №{board_request.pk}')
        # A card's notification names its code, never its title: so does this one.
        self.assertNotIn('Нарезать', note.title + note.message)
        self.assertTrue(NotificationDelivery.objects.filter(notification=note).exists())
        update_intake(self.board, actor=self.owner, enabled=True, handler_ids=[])
        self.submit('Вторая')
        self.assertEqual(
            list(self.events('BOARD_REQUEST_NEW').values_list('recipient__username', flat=True).order_by('pk')),
            ['member_one', 'pdo_owner'],
        )

    def test_every_decision_tells_the_author_by_bell_and_mail(self):
        accepted, rejected, duplicate = self.submit('A'), self.submit('B'), self.submit('C')
        card = accept_request(accepted, actor=self.member, assignee_ids=[self.member.pk])
        reject_request(rejected, actor=self.member, reason='Не наш профиль')
        mark_duplicate(duplicate, actor=self.member, card_code=card.code)
        for event_type, board_request, needle in (
            ('BOARD_REQUEST_ACCEPTED', accepted, f'карточка {card.code}'),
            ('BOARD_REQUEST_REJECTED', rejected, 'отклонена'),
            ('BOARD_REQUEST_DUPLICATE', duplicate, f'дубль карточки {card.code}'),
        ):
            with self.subTest(event_type=event_type):
                note = self.events(event_type).get()
                self.assertEqual((note.recipient, note.related_board_request), (self.requester, board_request))
                self.assertIn(needle, note.title)
                self.assertNotIn('Не наш профиль', note.title + note.message)
                self.assertTrue(NotificationDelivery.objects.filter(notification=note).exists())
                self.assertEqual(get_notification_url(note), reverse('boards:request_detail', args=[board_request.pk]))
        complete_card(card, actor=self.member, execution_comment='Сделано')
        done = self.events('BOARD_REQUEST_DONE').get()
        self.assertEqual((done.recipient, done.related_board_request), (self.requester, accepted))
        self.assertIn('выполнена', done.title)
        self.assertTrue(NotificationDelivery.objects.filter(notification=done).exists())

    def test_the_source_shape_clean_and_email_description(self):
        board_request = self.submit()
        note = self.events('BOARD_REQUEST_NEW').get()
        described = describe_notification_source(note)
        self.assertEqual(described['label'], f'Заявка №{board_request.pk}')
        self.assertEqual(described['context'], f'Доска «{self.board.name}»')
        self.assertIn('примите', get_required_action(note))
        note.related_task = task_of(self.card())
        with self.assertRaises(ValidationError):
            note.clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=note.pk).update(related_board_request=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=note.pk).update(source_type='TASK')


# --------------------------------------------------------------------------
# Live: `request_changed` and «Входящие (N)»
# --------------------------------------------------------------------------


class LiveTests(IntakeFixture, TestCase):
    def test_the_change_code_is_in_the_contract(self):
        self.assertIn(BOARD_CHANGE_REQUEST_CHANGED, BOARD_CHANGES)
        self.assertEqual(BOARD_CHANGE_REQUEST_CHANGED, 'request_changed')

    def test_every_request_write_publishes_it(self):
        self.enable()
        with mock.patch('boards.services.emit_board_updated') as emitted:
            board_request = self.submit()
            withdraw_request(board_request, actor=self.requester)
            card = accept_request(self.submit('B'), actor=self.member, assignee_ids=[self.member.pk])
            reject_request(self.submit('C'), actor=self.member, reason='Нет')
            mark_duplicate(self.submit('D'), actor=self.member, card_code=card.code)
        changes = [call.args[1] for call in emitted.call_args_list]
        self.assertEqual(changes.count('request_changed'), 8)
        self.assertEqual(set(changes), {'request_changed'})

    def test_the_counter_is_in_the_live_tabs_block(self):
        self.enable()
        self.client.force_login(self.member)
        before = self.client.get(fragment_url(self.board)).json()
        self.assertIn('Входящие (0)', before['tabs_html'])
        self.submit()
        after = self.client.get(fragment_url(self.board)).json()
        self.assertIn('Входящие (1)', after['tabs_html'])
        self.assertNotEqual(before['tabs_revision'], after['tabs_revision'])
        page = self.client.get(board_url(self.board)).content.decode()
        self.assertIn('Входящие (1)', page)
        # Nobody who does not work on the board is shown it.
        self.client.force_login(self.requester)
        self.assertEqual(self.client.get(fragment_url(self.board)).status_code, 200)  # widened access reads
        self.assertNotIn('Входящие', self.client.get(fragment_url(self.board)).json()['tabs_html'])

    def test_the_sync_revision_moves_with_a_request(self):
        from realtime.sync import build_sync_state

        self.enable()
        before = build_sync_state(self.member)['revisions']['boards']
        self.submit()
        self.assertNotEqual(build_sync_state(self.member)['revisions']['boards'], before)


class MyRequestsTests(IntakeFixture, TestCase):
    def test_rows_with_what_became_of_each(self):
        self.enable()
        accepted, rejected = self.submit('A'), self.submit('B')
        card = accept_request(accepted, actor=self.member, assignee_ids=[self.member.pk])
        reject_request(rejected, actor=self.member, reason='Нет')
        rows = {row['request'].title: row for row in build_my_requests(self.requester)}
        self.assertEqual(rows['A']['card']['code'], card.code)
        self.assertEqual(rows['A']['card']['state_label'], 'В работе')
        self.assertIsNone(rows['B']['card'])
        inbox = build_inbox(self.board, self.member)
        self.assertEqual(inbox['new_requests'], [])
        self.assertEqual(len(inbox['decided_rows']), 2)
