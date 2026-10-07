"""Stage 21 on PostgreSQL: deadline moves, reminders and the report, on
stage 21's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-21/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the refusal that writes nothing, the history row and its notifications, the
reminders and their idempotency, the report's aggregates, the table's
subquery and the number of queries with few and many moves.
"""
import datetime

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from boards.models import Board, BoardCard, BoardCardDueChange
from boards.selectors import build_board_state, build_board_table, build_deviation_report
from boards.services import DueReasonError, send_due_reminders, update_card
from notifications.models import Notification
from references.models import DeviationReason
from tasks.models import Task

board = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
main = board.sub_boards.order_by('position').first()
card = BoardCard.objects.get(board=board, number=1)
today = timezone.localdate()


def task_of(card):
    return Task.objects.get(board_card=card)


def move(card, days, reason=None, comment=''):
    task = task_of(card)
    card.refresh_from_db()
    return update_card(
        card, actor=admin, title=card.title, description=card.description,
        due_date=task.due_date + datetime.timedelta(days=days),
        assignee_ids=list(task.assignees.values_list('user_id', flat=True)),
        due_reason_id=DeviationReason.objects.get(code=reason).pk if reason else None, due_comment=comment,
    )


def page_queries():
    with CaptureQueriesContext(connection) as captured:
        build_board_state(board, main, admin, card_id=card.pk)
    return len(captured)


def report_queries():
    with CaptureQueriesContext(connection) as captured:
        report = build_deviation_report(board, date_from=today - datetime.timedelta(days=29), date_to=today)
        list(report['rows'])
    return len(captured)


print('причин в справочнике:', DeviationReason.objects.filter(is_active=True).count())
page_queries()
before_page, before_report = page_queries(), report_queries()
due_before = task_of(card).due_date
try:
    move(card, 3)
except DueReasonError as exc:
    print('без причины:', exc, '| срок не изменился:', task_of(card).due_date == due_before,
          '| строк истории:', BoardCardDueChange.objects.filter(card=card).count())
DeviationReason.objects.filter(code='OTHER').update(is_active=False)
try:
    move(card, 3, 'OTHER')
except DueReasonError as exc:
    print('выключенная причина:', exc)
DeviationReason.objects.filter(code='OTHER').update(is_active=True)
move(card, 3, 'MATERIAL', 'Лист со склада в пятницу.')
change = BoardCardDueChange.objects.filter(card=card).latest('pk')
print('перенос:', change.old_due, '→', change.new_due, change.reason.name, f'({change.shift_days:+d} дн.)',
      '| исходный срок:', card.original_due_date)
told = Notification.objects.filter(event_type='BOARD_DUE_CHANGED', deduplication_key__endswith=f'due_change:{change.pk}')
print('«Срок перенесён»:', sorted(told.values_list('recipient__username', flat=True)), '|', told.first().title,
      '| писем:', sum(note.deliveries.count() for note in told))
for _ in range(10):
    move(card, 1, 'DEFECT')
print('запросов страницы с открытой карточкой: до —', before_page, '| после 11 переносов —', page_queries())
print('запросов отчёта: до —', before_report, '| после 11 переносов —', report_queries())

report = build_deviation_report(board, date_from=today - datetime.timedelta(days=29), date_to=today)
print('сводка:', [(entry['reason'].name, entry['moves'], entry['shift'], entry['cards']) for entry in report['summary']])
print('итого:', report['moves'], 'переносов,', report['cards'], 'карточек, сдвиг', report['shift'], 'дн.')
rows = build_board_table(board, main, admin, sort='-changes')['rows']
print('«Таблица», по переносам:', [(row['card'].code, row['due_change_count'], row['last_due_reason']) for row in rows[:3]])

first = send_due_reminders()
second = send_due_reminders()
print('напоминания: первый запуск —', first, '| второй —', second)
for event in ('BOARD_DUE_SOON', 'BOARD_OVERDUE'):
    notes = Notification.objects.filter(event_type=event)
    print(event, sorted((note.title, note.recipient.username) for note in notes),
          '| писем поставлено:', sum(note.deliveries.count() for note in notes))
