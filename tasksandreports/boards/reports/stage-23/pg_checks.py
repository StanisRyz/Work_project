"""Stage 23 on PostgreSQL: «Норматив этапа», the traffic light, «Просрочен
этап», «Этапы», the report's «Этапы» and the digest, on stage 23's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-23/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the renamed column and its constraint, the lights against the filter, a
card's path, the report, the digest through `locmem`, and the number of
queries with few and many cards.
"""
import datetime

from django.contrib.auth.models import User
from django.core import mail
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from boards.digest import send_digests
from boards.models import Board, BoardCard, BoardColumn, BoardDigestSubscription
from boards.selectors import build_board_state, build_board_table, build_stage_report, parse_board_filters
from boards.services import BoardError, create_card, move_card, set_column_norm, set_digest_subscription
from ecosystem.workdays import working_days_between
from notifications.models import Notification

zap = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
main = zap.sub_boards.order_by('position').first()
todo = BoardColumn.objects.get(sub_board=main, name='Сделать')
review = BoardColumn.objects.get(sub_board=main, name='На проверке')
done = BoardColumn.objects.get(sub_board=main, is_done=True)
today = timezone.localdate()

with connection.cursor() as cursor:
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'boards_boardcolumn' AND column_name IN ('norm_working_days', 'stale_after_days')"
    )
    print('колонка в БД:', [row[0] for row in cursor.fetchall()])
print('нормативы:', {column.name: column.norm_working_days for column in main.columns.order_by('position')})
for value, column in ((0, todo), (366, todo), (2, done)):
    try:
        with transaction.atomic():
            BoardColumn.objects.filter(pk=column.pk).update(norm_working_days=value)
    except IntegrityError as error:
        print(f'ограничение БД ({column.name}, {value}):', type(error).__name__, str(error).split('\n')[0][:90])
for value, column in ((0, todo), (2, done)):
    try:
        set_column_norm(column, actor=admin, days=value)
    except BoardError as error:
        print(f'отказ сервиса ({column.name}, {value}):', error)

state = build_board_state(zap, main, admin)
lights = {item['card'].code: (item['light'], item['plan_exit'].strftime('%d.%m') if item['plan_exit'] else None)
          for column in state['columns'] if not column['is_done'] for item in column['cards']}
print('светофор:', lights)
late = build_board_state(zap, main, admin, filters=parse_board_filters({'stale': '1'}))
codes = sorted(item['card'].code for column in late['columns'] if not column['is_done'] for item in column['cards'])
print('«Просрочен этап»:', codes, '| совпадает с красными:',
      codes == sorted(code for code, (light, _) in lights.items() if light == 'red'))
print('р.д. пт → пн:', working_days_between(datetime.date(2026, 10, 9), datetime.date(2026, 10, 12)))

bracket = BoardCard.objects.get(board=zap, title='Изготовить кронштейн')
panel = build_board_state(zap, main, admin, card_id=bracket.pk)['card']
print('«Этапы» ZAP-11:', [(row['column'], row['days'], row['deviation']) for row in panel['stages']])

table = build_board_table(zap, main, admin, sort='-stage')
print('«Таблица» по отклонению этапа:', [(row['card'].code, row.get('stage_deviation')) for row in table['rows']][:5])

report = build_stage_report(zap, date_from=today - datetime.timedelta(days=29), date_to=today)
print('отчёт «Этапы»:', [(f"{row['sub_board'].name}/{row['column'].name}", row['exits'], row['average'],
                          row['longest'], row['within_share'], row['late_now'])
                         for row in report['stage_rows'] if row['exits'] or row['late_now']])

set_digest_subscription(zap, actor=ivanov, frequency='DAILY')
set_digest_subscription(zap, actor=admin, frequency='WEEKLY')
print('подписки на дайджест:', sorted(BoardDigestSubscription.objects.values_list('user__username', 'frequency')))
notifications = Notification.objects.count()
with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True, EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend'):
    mail.outbox = []
    monday = today + datetime.timedelta(days=(7 - today.weekday()) % 7 or 7)
    first = send_digests(monday)
    second = send_digests(monday)
    print('дайджест в понедельник:', {k: v for k, v in first.items() if k != 'errors'},
          '| повтор:', second['sent'], '| писем:', len(mail.outbox), [m.to[0] for m in mail.outbox])
    print('уведомлений не прибавилось:', Notification.objects.count() == notifications)
with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
    try:
        send_digests(monday)
    except Exception as error:  # noqa: BLE001 - printed
        print('почта выключена:', type(error).__name__, error)


def page_queries():
    with CaptureQueriesContext(connection) as queries:
        build_board_state(zap, main, admin, card_id=bracket.pk)
    return len(queries)


def report_queries():
    with CaptureQueriesContext(connection) as queries:
        build_stage_report(zap, date_from=today - datetime.timedelta(days=29), date_to=today)
    return len(queries)


before, report_before = page_queries(), report_queries()
for index in range(8):
    card = create_card(main, actor=admin, title=f'Поток {index}', due_date=today + datetime.timedelta(days=9),
                       assignee_ids=[ivanov.pk])
    move_card(card, actor=admin, column=review)
print('запросов доски с открытой карточкой: до —', before, '| после 8 карточек —', page_queries())
print('запросов отчёта «Этапы»: до —', report_before, '| после 8 карточек —', report_queries())
