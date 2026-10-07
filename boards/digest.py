"""«Дайджест на почту»: a summary of a person's boards, mailed by
`manage.py board_digest`.

A head does not open the system every day; the digest brings the state of
their boards to them. It is **not** a `Notification`: a notification is one
business fact addressed to the people it concerns, deduplicated per fact and
shown in the bell; the digest is a periodic summary of facts already
recorded — a report — and turning it into notifications would put a
«дайджест» entry in the bell for something nobody did. It is sent exactly as
`notifications/management/commands/send_welcome_email.py` sends its letter:
straight through the configured backend, one personalized message per
person, failures scrubbed by `notifications.email_delivery.sanitize_error()`,
counters only in the log.

Who gets what:

* `BoardDigestSubscription.frequency` — `DAILY` on every working day
  (Monday–Friday), `WEEKLY` on Mondays;
* one letter per person for every board they are due a digest of today and
  still read (`can_view_board()`, a live board), in sections by board;
* each board's section covers the period since that subscription was last
  handled (`last_sent_on`), else the last day (`DAILY`) or week (`WEEKLY`):
  open cards past their срок, open cards past their stage plan («Просрочен
  этап»), open cards waiting for an open card, the moves of a срок in the
  period with their reasons, the number completed in the period, and links to
  the board and to its «Таблица» under the matching filter;
* a letter with nothing in it but «выполнено: 0» is not sent; its
  subscriptions are still marked handled for the day, so the next digest
  covers from here;
* `last_sent_on = today` is written after a successful send (or an empty
  digest), so a second run the same day sends nothing; a failed send leaves
  it, and the next run tries again.
"""

import datetime
import logging
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from accounts.templatetags.people import person_name
from ecosystem.logging_utils import log_event
from ecosystem.workdays import working_days_between

from .models import BoardCardDueChange, BoardColumn, BoardDigestSubscription
from .permissions import can_view_board, is_active_employee
from .selectors import (
    blocked_condition,
    blocker_codes,
    blocker_codes_annotation,
    in_column_since,
    stage_light,
    stage_plan,
)


logger = logging.getLogger('notifications.email')

SUBJECT = '[Экосистема качества] Дайджест досок за {date}'
# How far back a digest looks the first time, by frequency.
FIRST_PERIOD_DAYS = {
    BoardDigestSubscription.Frequency.DAILY: 1,
    BoardDigestSubscription.Frequency.WEEKLY: 7,
}
# At most this many cards a section lists; the rest are counted.
SECTION_LIMIT = 30


class DigestDisabled(Exception):
    """Mail is switched off (`EMAIL_NOTIFICATIONS_ENABLED=false`)."""


def due_frequencies(today):
    """The frequencies due on `today`: `DAILY` on a working day, `WEEKLY` on
    a Monday (which is a working day too)."""
    if today.weekday() >= 5:
        return []
    due = [BoardDigestSubscription.Frequency.DAILY]
    if today.weekday() == 0:
        due.append(BoardDigestSubscription.Frequency.WEEKLY)
    return due


def period_start(subscription, today):
    """The first day the section of `subscription` covers: the day it was
    last handled, else — the first time — the previous working day for
    `DAILY` (Friday on a Monday, so the weekend is in it) and a week back for
    `WEEKLY`. The period ends yesterday: today is not over yet."""
    if subscription.last_sent_on is not None and subscription.last_sent_on < today:
        return subscription.last_sent_on
    start = today - datetime.timedelta(days=FIRST_PERIOD_DAYS[subscription.frequency])
    while start.weekday() >= 5:
        start -= datetime.timedelta(days=1)
    return start


def _absolute(path):
    return urljoin(f"{settings.APP_BASE_URL.rstrip('/')}/", path.lstrip('/'))


def _start_of_day(day):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def _board_tasks(board):
    from tasks.models import Task

    return Task.objects.filter(source_type=Task.SourceType.BOARD, board_card__board=board)


def _limited(rows):
    return rows[:SECTION_LIMIT], max(len(rows) - SECTION_LIMIT, 0)


def board_section(subscription, user, today):
    """One board's part of a letter, or `None` when `user` no longer reads a
    live board. Every list is the board's own; a card of another board named
    as a blocker appears by its code only when `user` reads that board."""
    from django.db.models import Prefetch

    from tasks.models import TaskAssignee

    board = subscription.board
    if board.is_archived or not can_view_board(user, board):
        return None
    since = period_start(subscription, today)
    start, end = _start_of_day(since), _start_of_day(today)
    open_tasks = _board_tasks(board).filter(status__code='IN_PROGRESS')

    overdue = [
        {
            'code': f'{board.code}-{task.board_card.number}',
            'title': task.board_card.title,
            'due_date': task.due_date,
            'assignees': ', '.join(person_name(row.user) for row in task.assignees.all()),
        }
        for task in open_tasks.filter(due_date__lt=today)
        .select_related('board_card')
        .prefetch_related(Prefetch('assignees', queryset=TaskAssignee.objects.select_related('user')))
        .order_by('due_date', 'board_card__number')
    ]

    columns = {column.pk: column for column in BoardColumn.objects.filter(sub_board__board=board)}
    first_working = {}
    for column in sorted(columns.values(), key=lambda column: (column.position, column.pk)):
        if not column.is_done:
            first_working.setdefault(column.sub_board_id, column)
    late = []
    for task in (
        open_tasks.filter(board_card__parent__isnull=True)
        .select_related('board_card')
        .annotate(since=in_column_since())
        .order_by('board_card__number')
    ):
        card = task.board_card
        column = columns.get(card.column_id) or first_working.get(card.sub_board_id)
        if column is None or not column.norm_working_days:
            continue
        plan = stage_plan(task.since, column.norm_working_days)
        if stage_light(plan, today) == 'red':
            late.append({
                'code': f'{board.code}-{card.number}',
                'title': card.title,
                'column': column.name,
                'plan': plan,
                'late_by': working_days_between(plan, today),
            })
    late.sort(key=lambda row: -row['late_by'])

    blocked = [
        {
            'code': f'{board.code}-{task.board_card.number}',
            'title': task.board_card.title,
            'waits_for': blocker_codes(task.waits) or 'карточку другой доски',
        }
        for task in open_tasks.filter(blocked_condition())
        .select_related('board_card')
        .annotate(waits=blocker_codes_annotation(user))
        .order_by('board_card__number')
    ]

    moves = [
        {
            'code': f'{board.code}-{change.card.number}',
            'title': change.card.title,
            'old_due': change.old_due,
            'new_due': change.new_due,
            'reason': change.reason.name if change.reason_id else '',
        }
        for change in BoardCardDueChange.objects.filter(
            card__board=board, changed_at__gte=start, changed_at__lt=end,
        ).select_related('card', 'reason').order_by('changed_at', 'pk')
    ]

    completed = _board_tasks(board).filter(
        status__code='COMPLETED', completed_at__gte=start, completed_at__lt=end,
    ).count()

    board_url = reverse('boards:detail', args=[board.pk])
    overdue, overdue_more = _limited(overdue)
    late, late_more = _limited(late)
    blocked, blocked_more = _limited(blocked)
    moves, moves_more = _limited(moves)
    return {
        'board': board,
        'since': since,
        'until': today - datetime.timedelta(days=1),
        'overdue': overdue,
        'overdue_more': overdue_more,
        'late': late,
        'late_more': late_more,
        'blocked': blocked,
        'blocked_more': blocked_more,
        'moves': moves,
        'moves_more': moves_more,
        'completed': completed,
        'board_url': _absolute(board_url),
        'overdue_url': _absolute(f'{board_url}?view=table&overdue=1'),
        'late_url': _absolute(f'{board_url}?view=table&stale=1&sort=-stage'),
        'blocked_url': _absolute(f'{board_url}?view=table&blocked=1'),
        'is_empty': not (overdue or late or blocked or moves or completed),
    }


def _send(user, sections, today):
    context = {
        'user_name': person_name(user),
        'sections': sections,
        'today': today,
        'app_url': settings.APP_BASE_URL,
    }
    message = EmailMultiAlternatives(
        subject=SUBJECT.format(date=today.strftime('%d.%m.%Y')),
        body=render_to_string('boards/email/digest.txt', context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    message.attach_alternative(render_to_string('boards/email/digest.html', context), 'text/html')
    if message.send(fail_silently=False) != 1:
        raise RuntimeError('Почтовый backend не подтвердил отправку сообщения.')


def send_digests(today=None):
    """Mail every digest due on `today` (the local date by default).

    Returns the counts — `sent` letters, `empty` (nothing to report, not
    sent), `skipped_no_email`, `skipped_inactive`, `failed` — and `errors`,
    each failure scrubbed by `sanitize_error()` for the operator's console.
    Refuses — `DigestDisabled` — while mail is switched off. Logs the counts
    and, per failure, the user id and the error's type — never an address, a
    subject, a body or an error text.
    """
    from notifications.email_delivery import sanitize_error

    if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        raise DigestDisabled('EMAIL_NOTIFICATIONS_ENABLED=false: отправка писем отключена.')
    today = today or timezone.localdate()
    summary = {'sent': 0, 'empty': 0, 'skipped_no_email': 0, 'skipped_inactive': 0, 'failed': 0}
    errors = []
    frequencies = due_frequencies(today)
    if not frequencies:
        log_event(logger, 'INFO', 'board.digest', outcome='not_due', **summary)
        return {**summary, 'errors': errors}
    subscriptions = (
        BoardDigestSubscription.objects.filter(frequency__in=frequencies)
        .exclude(last_sent_on=today)
        .select_related('board', 'user')
        .order_by('user_id', 'board__name', 'board_id')
    )
    by_user = {}
    for subscription in subscriptions:
        by_user.setdefault(subscription.user_id, []).append(subscription)
    users = {user.pk: user for user in get_user_model().objects.filter(pk__in=by_user)}
    for user_id, user_subscriptions in by_user.items():
        user = users[user_id]
        if not is_active_employee(user):
            summary['skipped_inactive'] += 1
            continue
        if not (user.email or '').strip():
            summary['skipped_no_email'] += 1
            continue
        sections = [
            section for section in (board_section(subscription, user, today) for subscription in user_subscriptions)
            if section is not None
        ]
        handled = [subscription.pk for subscription in user_subscriptions]
        if all(section['is_empty'] for section in sections):
            BoardDigestSubscription.objects.filter(pk__in=handled).update(last_sent_on=today)
            summary['empty'] += 1
            continue
        try:
            _send(user, [section for section in sections if not section['is_empty']], today)
        except Exception as exc:  # noqa: BLE001 - every failure is counted and the run goes on
            summary['failed'] += 1
            log_event(
                logger, 'ERROR', 'board.digest_failed',
                user_id=user.pk, error_type=type(exc).__name__, outcome='failed',
            )
            errors.append(sanitize_error(exc))
            continue
        BoardDigestSubscription.objects.filter(pk__in=handled).update(last_sent_on=today)
        summary['sent'] += 1
    log_event(logger, 'INFO', 'board.digest', outcome='ok', **summary)
    return {**summary, 'errors': errors}
