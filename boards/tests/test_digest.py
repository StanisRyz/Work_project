"""«Дайджест на почту» (stage 23): `BoardDigestSubscription`,
`set_digest_subscription()`, `boards.digest.send_digests()` and `manage.py
board_digest` — sent through the `locmem` backend, never a `Notification`.

`today` is fixed at Monday 12.10.2026 (both frequencies due) unless a test
says otherwise; the board's facts are dated around it.
"""

import datetime
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from notifications.models import Notification
from tasks.models import Task

from ..digest import DigestDisabled, due_frequencies, send_digests
from ..models import BoardCardDueChange, BoardCardEvent, BoardDigestSubscription
from ..services import (
    LINK_WAITS,
    BoardError,
    archive_board,
    complete_card,
    link_cards,
    remove_board_member,
    set_column_norm,
    set_digest_subscription,
)
from .helpers import BoardFixtureMixin, board_url, make_user, reason_id
from .test_journal import task_of
from .test_lifecycle import main_of


MONDAY = datetime.date(2026, 10, 12)
TUESDAY = datetime.date(2026, 10, 13)
SATURDAY = datetime.date(2026, 10, 17)
LOCMEM = override_settings(
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://quality.example',
)


def noon(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(12, 0)))


class DigestFixture(BoardFixtureMixin):
    def setUp(self):
        self.head = make_user('board_head', UserProfile.Role.MANAGER)
        self.head.email = 'head@example.com'
        self.head.save()
        from ..services import add_board_members

        add_board_members(self.board, [self.head.pk], actor=self.owner)

    def subscribe(self, frequency='DAILY', user=None):
        return set_digest_subscription(self.board, actor=user or self.head, frequency=frequency)

    def eventful(self):
        """Every section of the letter has something: an overdue card, one
        past its stage, one waiting, a moved срок, one completed."""
        overdue = self.card('Просроченная')
        Task.objects.filter(pk=task_of(overdue).pk).update(due_date=datetime.date(2026, 10, 1))
        late = self.card('Застряла в этапе')
        set_column_norm(self.column('TODO'), actor=self.owner, days=2)
        BoardCardEvent.objects.filter(card=late, kind='CREATED').update(created_at=noon(datetime.date(2026, 9, 1)))
        waiting = self.card('Ждёт металл')
        blocker = self.card('Металл')
        link_cards(waiting, actor=self.member, other_code=blocker.code, kind=LINK_WAITS)
        BoardCardDueChange.objects.create(
            card=waiting, old_due=datetime.date(2026, 10, 10), new_due=datetime.date(2026, 10, 20),
            reason_id=reason_id(), changed_by=self.member,
        )
        BoardCardDueChange.objects.filter(card=waiting).update(changed_at=noon(datetime.date(2026, 10, 9)))
        done = self.card('Сделано')
        complete_card(done, actor=self.member, execution_comment='Да')
        Task.objects.filter(pk=task_of(done).pk).update(completed_at=noon(datetime.date(2026, 10, 10)))
        return {'overdue': overdue, 'late': late, 'waiting': waiting, 'blocker': blocker, 'done': done}


class SubscriptionTests(DigestFixture, TestCase):
    def test_set_change_and_switch_off(self):
        self.assertEqual(self.subscribe('DAILY'), 'DAILY')
        self.assertEqual(self.subscribe('weekly'), 'WEEKLY')
        self.assertEqual(BoardDigestSubscription.objects.get().frequency, 'WEEKLY')
        self.assertEqual(self.subscribe(''), '')
        self.assertFalse(BoardDigestSubscription.objects.exists())
        with self.assertRaisesMessage(BoardError, 'как часто'):
            self.subscribe('HOURLY')

    def test_any_reader_but_not_a_stranger_or_an_archived_board(self):
        self.assertEqual(self.subscribe('DAILY', user=self.outsider), 'DAILY')
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            with self.assertRaisesMessage(BoardError, 'читатели'):
                self.subscribe('DAILY', user=make_user('digest_stranger'))
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'в архиве'):
            self.subscribe('WEEKLY')

    def test_removing_a_member_drops_it(self):
        self.subscribe('DAILY')
        remove_board_member(self.board, self.head, actor=self.owner)
        self.assertFalse(BoardDigestSubscription.objects.filter(user=self.head).exists())

    def test_the_menu_and_the_route(self):
        self.client.force_login(self.head)
        page = main_of(self.client.get(board_url(self.board)))
        self.assertIn('Дайджест на почту', page)
        url = reverse('boards:digest', args=[self.board.pk])
        response = self.client.post(f'{url}?sub={self.main.pk}', {'frequency': 'WEEKLY'})
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        self.assertEqual(BoardDigestSubscription.objects.get(user=self.head).frequency, 'WEEKLY')
        self.assertIn('<option value="WEEKLY" selected>', main_of(self.client.get(board_url(self.board))))
        self.client.get(url)  # a GET changes nothing
        self.assertTrue(BoardDigestSubscription.objects.filter(user=self.head).exists())
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            self.client.force_login(make_user('digest_route_stranger'))
            for method in (self.client.get, self.client.post):
                self.assertEqual(method(url, {'frequency': 'DAILY'}).status_code, 403)


@LOCMEM
class SendTests(DigestFixture, TestCase):
    def test_daily_on_working_days_weekly_on_mondays(self):
        self.assertEqual(due_frequencies(MONDAY), ['DAILY', 'WEEKLY'])
        self.assertEqual(due_frequencies(TUESDAY), ['DAILY'])
        self.assertEqual(due_frequencies(SATURDAY), [])
        self.eventful()
        self.subscribe('WEEKLY')
        self.assertEqual(send_digests(TUESDAY)['sent'], 0)
        self.assertEqual(send_digests(SATURDAY)['sent'], 0)
        self.assertEqual(send_digests(MONDAY)['sent'], 1)
        self.subscribe('DAILY', user=self.member)
        self.member.email = 'member@example.com'
        self.member.save()
        self.assertEqual(send_digests(TUESDAY)['sent'], 1)
        self.assertEqual(mail.outbox[-1].to, ['member@example.com'])

    def test_a_second_run_the_same_day_sends_nothing(self):
        self.eventful()
        self.subscribe('DAILY')
        self.assertEqual(send_digests(MONDAY)['sent'], 1)
        self.assertEqual(send_digests(MONDAY)['sent'], 0)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(BoardDigestSubscription.objects.get().last_sent_on, MONDAY)

    def test_the_letter_holds_every_section_and_no_notification_is_made(self):
        cards = self.eventful()
        self.subscribe('DAILY')
        notifications = Notification.objects.count()
        send_digests(MONDAY)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['head@example.com'])
        self.assertIn('12.10.2026', message.subject)
        body = message.body
        for needle in (
            'Просроченные карточки', cards['overdue'].code, '01.10.2026',
            'Просрочен этап', cards['late'].code,
            'Заблокированные', cards['waiting'].code, f'ждёт {cards["blocker"].code}',
            'Переносы срока за период', '10.10.2026 → 20.10.2026',
            'Выполнено за период: 1',
            f'https://quality.example/work/boards/{self.board.pk}/?view=table&overdue=1',
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        html = message.alternatives[0][0]
        self.assertIn('Просрочен этап', html)
        self.assertIn(cards['late'].code, html)
        # The period of a first daily digest on a Monday starts on Friday.
        self.assertIn('09.10.2026 — 11.10.2026', body)
        self.assertEqual(Notification.objects.count(), notifications)

    def test_an_empty_digest_is_not_sent(self):
        self.card('Обычная')
        self.subscribe('DAILY')
        summary = send_digests(MONDAY)
        self.assertEqual((summary['sent'], summary['empty']), (0, 1))
        self.assertEqual(mail.outbox, [])
        self.assertEqual(BoardDigestSubscription.objects.get().last_sent_on, MONDAY)

    def test_a_board_no_longer_read_is_left_out(self):
        self.eventful()
        self.subscribe('DAILY')
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            # Taken off the board by hand, the subscription row left behind.
            self.board.members.filter(user=self.head).delete()
            summary = send_digests(MONDAY)
        self.assertEqual((summary['sent'], summary['empty']), (0, 1))
        self.assertEqual(mail.outbox, [])

    def test_no_address_is_skipped_and_retried(self):
        self.eventful()
        self.subscribe('DAILY')
        self.head.email = ''
        self.head.save()
        self.assertEqual(send_digests(MONDAY)['skipped_no_email'], 1)
        self.assertIsNone(BoardDigestSubscription.objects.get().last_sent_on)

    def test_a_failure_is_counted_scrubbed_and_logged_without_the_address(self):
        self.eventful()
        self.subscribe('DAILY')
        with mock.patch(
            'django.core.mail.EmailMultiAlternatives.send',
            side_effect=OSError('relay refused head@example.com'),
        ), self.assertLogs('notifications.email', level='INFO') as logs:
            summary = send_digests(MONDAY)
        self.assertEqual(summary['failed'], 1)
        self.assertTrue(summary['errors'][0].startswith('OSError'))
        self.assertIsNone(BoardDigestSubscription.objects.get().last_sent_on)
        joined = '\n'.join(logs.output)
        self.assertNotIn('head@example.com', joined)
        self.assertIn('board.digest_failed', joined)

    def test_the_log_carries_counters_only(self):
        self.eventful()
        self.subscribe('DAILY')
        with self.assertLogs('notifications.email', level='INFO') as logs:
            send_digests(MONDAY)
        joined = '\n'.join(logs.output)
        self.assertIn('sent=1', joined)
        self.assertNotIn('head@example.com', joined)
        self.assertNotIn('Просроч', joined)

    def test_the_command(self):
        self.eventful()
        self.subscribe('DAILY')
        out = StringIO()
        call_command('board_digest', '--date', MONDAY.isoformat(), stdout=out)
        self.assertIn('отправлено — 1', out.getvalue())
        with self.assertRaisesMessage(CommandError, 'ГГГГ-ММ-ДД'):
            call_command('board_digest', '--date', '12.10.2026', stdout=StringIO())


class DisabledTests(DigestFixture, TestCase):
    @override_settings(EMAIL_NOTIFICATIONS_ENABLED=False)
    def test_mail_switched_off_is_a_refusal(self):
        self.subscribe('DAILY')
        with self.assertRaises(DigestDisabled):
            send_digests(MONDAY)
        with self.assertRaisesMessage(CommandError, 'EMAIL_NOTIFICATIONS_ENABLED=false'):
            call_command('board_digest', stdout=StringIO())
        self.assertIsNone(BoardDigestSubscription.objects.get().last_sent_on)
