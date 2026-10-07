"""Stage 22 on PostgreSQL: links, blocking, column subscriptions and sums, on
stage 22's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-22/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the link by a Cyrillic code, the constraints, the refusals, «можно начинать»
only after the last blocker, «вошла в колонку» once per person, the column
sums with and without a filter, «Итого», and the number of queries with few
and many links and cards.
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from boards.models import Board, BoardCard, BoardCardLink, BoardColumn, BoardColumnSubscription, BoardField
from boards.selectors import build_board_state, build_board_table, card_links, parse_board_filters
from boards.services import (
    LINK_WAITS,
    BoardError,
    complete_card,
    create_card,
    link_cards,
    move_card,
    reopen_card,
    toggle_column_subscription,
    update_field,
)
from notifications.models import Notification, NotificationDelivery

zap = Board.objects.get(code='ZAP')
snb = Board.objects.get(code='СНБ')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
worker1 = User.objects.get(username='worker1')
worker2 = User.objects.get(username='worker2')
main = zap.sub_boards.order_by('position').first()
zap1 = BoardCard.objects.get(board=zap, number=1)
snb1 = BoardCard.objects.get(board=snb, number=1)
snb_sub = snb.sub_boards.get()
in_work = BoardColumn.objects.get(sub_board=main, name='В работе')

# Links by code, Cyrillic, any case.
link = link_cards(zap1, actor=admin, other_code='снб-1', kind=LINK_WAITS)
print('связь:', link.kind, link.from_card.code, '→', link.to_card.code)
snb2 = create_card(snb_sub, actor=admin, title='Закупить крепёж', due_date=snb1.tasks.get().due_date,
                   assignee_ids=[worker1.pk])
link_cards(zap1, actor=admin, other_code=snb2.code, kind=LINK_WAITS)
for code, actor in (('СНБ-1', admin), ('СНБ-999', admin), (snb1.code, worker2)):
    try:
        link_cards(zap1, actor=actor, other_code=code, kind=LINK_WAITS)
    except BoardError as exc:
        print(f'отказ {code} ({actor.username}):', exc)
try:
    link_cards(snb1, actor=admin, other_code=zap1.code, kind=LINK_WAITS)
except BoardError as exc:
    print('цикл:', exc)
for kwargs in ({'from_card': zap1, 'to_card': zap1, 'kind': 'RELATES'},
               {'from_card': snb1, 'to_card': zap1, 'kind': 'BLOCKS'}):
    try:
        with transaction.atomic():
            BoardCardLink.objects.create(created_by=admin, **kwargs)
    except IntegrityError as exc:
        print('ограничение БД:', type(exc).__name__, str(exc).split('\n')[0][:90])

state = build_board_state(zap, main, ivanov)
tile = next(item for column in state['columns'] for item in column['cards'] if item['card'].pk == zap1.pk)
print('плитка:', tile['blocker_count'], tile['first_blocker_code'], '+', tile['blocker_more'])
worker2_state = build_board_state(zap, main, worker2)
tile = next(item for column in worker2_state['columns'] for item in column['cards'] if item['card'].pk == zap1.pk)
print('плитка для worker2 (не читает СНБ):', tile['blocker_count'], repr(tile['first_blocker_code']))
blocked = build_board_state(zap, main, ivanov, filters=parse_board_filters({'blocked': '1'}))
print('«Заблокированные»:', [item['card'].code for column in blocked['columns'] if not column['is_done']
                              for item in column['cards']])
links = card_links(zap1, zap, main, worker2, can_work=True)
print('«Связи» для worker2:', [(group['label'], [row['code'] or row['readable'] for row in group['rows']])
                               for group in links['link_groups']])

complete_card(snb1, actor=worker1, execution_comment='Лист на складе')
print('после первой блокирующей «можно начинать»:', Notification.objects.filter(event_type='BOARD_UNBLOCKED').count())
complete_card(snb2, actor=worker1, execution_comment='Крепёж на складе')
notes = Notification.objects.filter(event_type='BOARD_UNBLOCKED')
print('после последней:', [(note.recipient.username, note.title) for note in notes],
      '| писем:', NotificationDelivery.objects.filter(notification__in=notes).count())
reopen_card(snb2, actor=admin)
print('после возврата в работу:', notes.count(), '| ждёт снова:',
      BoardCardLink.objects.filter(to_card=zap1, kind='BLOCKS', from_card__tasks__status__code='IN_PROGRESS').count())

# A column followed by a master.
toggle_column_subscription(in_work, actor=worker2)
zap2 = BoardCard.objects.get(board=zap, number=2)
move_card(zap2, actor=admin, column=in_work)
created = create_card(main, actor=admin, title='Новый заказ', due_date=zap1.tasks.get().due_date,
                      assignee_ids=[worker2.pk], column=in_work)
entered = Notification.objects.filter(event_type='BOARD_COLUMN_ENTERED')
print('«вошла в колонку»:', [(note.recipient.username, note.title) for note in entered],
      '| писем:', NotificationDelivery.objects.filter(notification__in=entered).count())
print('worker2 по новой карточке:', list(Notification.objects.filter(
    recipient=worker2, related_task__board_card=created).values_list('event_type', flat=True)))

# Sums.
amount = BoardField.objects.get(board=zap, name='Сумма')
update_field(amount, actor=admin, name=amount.name, show_on_tile=amount.show_on_tile, sum_in_column=True)
state = build_board_state(zap, main, admin)
print('суммы:', {row['name']: [item['text'] for item in row['sums']] for row in state['columns']})
state = build_board_state(zap, main, admin, filters=parse_board_filters({'q': 'заказ'}))
print('суммы с фильтром «заказ»:', {row['name']: [item['text'] for item in row['sums']] for row in state['columns']})
table = build_board_table(zap, main, admin)
print('«Итого»:', [cell['text'] for cell in table['total_cells'] if cell['text']],
      '| «Ждёт» у ZAP-1:', next(row['waits_for'] for row in table['rows'] if row['card'].pk == zap1.pk))


def page_queries():
    with CaptureQueriesContext(connection) as queries:
        build_board_state(zap, main, admin, card_id=zap1.pk)
    return len(queries)


before = page_queries()
for index in range(6):
    other = create_card(snb_sub, actor=admin, title=f'Ещё {index}', due_date=zap1.tasks.get().due_date,
                        assignee_ids=[worker1.pk])
    link_cards(zap1, actor=admin, other_code=other.code, kind=LINK_WAITS)
    create_card(main, actor=admin, title=f'Карточка {index}', due_date=zap1.tasks.get().due_date,
                assignee_ids=[ivanov.pk])
print('запросов доски с открытой карточкой: до —', before, '| после 6 связей и 6 карточек —', page_queries())
print('подписок на колонки:', BoardColumnSubscription.objects.count())
