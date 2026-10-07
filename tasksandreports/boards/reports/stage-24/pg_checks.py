"""Stage 24 on PostgreSQL: «Приём заявок», requests, «Входящие» and the
`REQUEST` notification source, on stage 24's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-24/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the intake set up, a request filed by a stranger to the board with its
fields parsed, a refusal per rule, accept / reject / duplicate with their
notifications, the constraints of `BoardRequest` and of the notification
source shape, and the query counts of «Заявки» and «Входящие».
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from boards.models import Board, BoardCardComment, BoardRequest
from boards.services import (
    BoardError,
    FieldValueError,
    accept_request,
    complete_card,
    mark_duplicate,
    reject_request,
    submit_request,
    update_intake,
)
from notifications.models import Notification, NotificationDelivery

zap = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
master = User.objects.get(username='otk_master')
number, priority = zap.fields.get(name='Номер заявки'), zap.fields.get(name='Приоритет')
high = priority.options.get(label='Высокий')

print('изменено:', update_intake(
    zap, actor=admin, enabled=True, due_days=3, hint='Номер заявки 1С обязателен',
    handler_ids=[ivanov.pk], form_fields={number.pk: (True, True), priority.pk: (True, False)},
))
print('то же самое ещё раз:', update_intake(
    zap, actor=admin, enabled=True, due_days=3, hint='Номер заявки 1С обязателен',
    handler_ids=[ivanov.pk], form_fields={number.pk: (True, True), priority.pk: (True, False)},
))
print('мастер ОТК — участник ZAP:', zap.members.filter(user=master).exists())
for values, title in (({priority.pk: high.pk}, 'Без номера'), ({number.pk: 'x' * 600}, 'Длинный номер')):
    try:
        submit_request(zap, author=master, title=title, field_values=values)
    except FieldValueError as error:
        print('отказ:', error)
first = submit_request(
    zap, author=master, title='Нарезать заготовки', description='Партия 40 шт.',
    field_values={number.pk: ' 3-1601 ', priority.pk: str(high.pk), 999999: 'чужое'},
)
print('заявка:', first.label, first.status, first.field_values)
print('«Новая заявка»:', list(Notification.objects.filter(event_type='BOARD_REQUEST_NEW')
                                 .values_list('recipient__username', 'source_type', 'title')),
      '| писем:', NotificationDelivery.objects.filter(notification__event_type='BOARD_REQUEST_NEW').count())

card = accept_request(first, actor=ivanov, assignee_ids=[ivanov.pk])
task = card.tasks.get()
print('принята:', card.code, card.column.name, task.due_date, '| поля:',
      sorted(card.field_values.values_list('field__name', flat=True)))
print('первое сообщение:', BoardCardComment.objects.get(card=card).text)
print('журнал:', card.events.get(kind='CREATED').details)
try:
    reject_request(first, actor=ivanov, reason='Повтор')
except BoardError as error:
    print('повтор:', error)
second = submit_request(zap, author=master, title='Покрасить корпус', field_values={number.pk: '3-1602'})
try:
    reject_request(second, actor=ivanov, reason='  ')
except BoardError as error:
    print('без причины:', error)
reject_request(second, actor=ivanov, reason='Не наш участок')
third = submit_request(zap, author=master, title='Нарезать ещё раз', field_values={number.pk: '3-1601'})
for code in ('СНБ-1', card.code.lower()):
    try:
        mark_duplicate(third, actor=ivanov, card_code=code)
        print('дубль:', code, '→', BoardRequest.objects.get(pk=third.pk).duplicate_of.code)
    except BoardError as error:
        print('дубль', code, '— отказ:', error)
complete_card(card, actor=ivanov, execution_comment='Нарезано')
print('автору:', list(Notification.objects.filter(recipient=master).order_by('pk')
                      .values_list('event_type', 'title')))
print('писем автору:', NotificationDelivery.objects.filter(notification__recipient=master).count())

now = timezone.now()
for status, extra in (('ACCEPTED', {}), ('REJECTED', {'decision_comment': ''}), ('NEW', {'decided_at': now})):
    try:
        with transaction.atomic():
            BoardRequest.objects.filter(pk=second.pk).update(status=status, **extra)
    except IntegrityError as error:
        print(f'ограничение ({status}):', str(error).split('\n')[0][:110])
note = Notification.objects.filter(event_type='BOARD_REQUEST_NEW').first()
for change in ({'related_board_request': None}, {'source_type': 'TASK'}, {'related_task': task}):
    try:
        with transaction.atomic():
            Notification.objects.filter(pk=note.pk).update(**change)
    except IntegrityError as error:
        print('форма источника:', list(change), str(error).split('\n')[0][:90])
print('старые уведомления других источников:', dict(
    Notification.objects.exclude(source_type='REQUEST').values_list('source_type').order_by()
    .annotate(n=__import__('django.db.models', fromlist=['Count']).Count('pk')).values_list('source_type', 'n')
))


def page_queries(user, url):
    client = Client()
    client.force_login(user)
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url, HTTP_HOST='127.0.0.1').status_code == 200
    return len(queries)


before = page_queries(master, '/work/requests/'), page_queries(ivanov, '/work/boards/1/inbox/')
for index in range(8):
    extra = submit_request(zap, author=master, title=f'Поток {index}', field_values={number.pk: f'4-{index}'})
    if index % 2:
        accept_request(extra, actor=ivanov, assignee_ids=[ivanov.pk])
after = page_queries(master, '/work/requests/'), page_queries(ivanov, '/work/boards/1/inbox/')
print('запросов «Заявки» / «Входящие»: до —', before, '| после 8 заявок —', after)
