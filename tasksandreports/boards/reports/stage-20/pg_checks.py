"""Stage 20 on PostgreSQL: subtasks through the services, on stage 20's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-20/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the constraint, the one number series, the warnings' `StringAgg`, the counts
and the number of queries of a page with 0 and 20 subtasks.
"""
from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test.utils import CaptureQueriesContext

from boards.models import Board, BoardCard, BoardCardEvent
from boards.selectors import build_board_state, column_counts
from boards.services import (
    BoardError, checklist_item_to_subtask, complete_card, create_subtask, create_subtasks_from_list,
    move_card,
)
from notifications.models import Notification
from tasks.models import Task

board = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
main = board.sub_boards.order_by('position').first()
card = BoardCard.objects.get(board=board, number=1)


def queries():
    with CaptureQueriesContext(connection) as captured:
        build_board_state(board, main, admin, card_id=card.pk)
    return len(captured)


before_counts = column_counts(main, admin)
queries()  # warm: the first read of a process caches what the next ones reuse
empty = queries()
subtasks = create_subtasks_from_list(card, actor=admin, text='Корпус\n\nКрышка\nКрепёж')
print('списком:', [(child.code, child.position, child.column_id) for child in subtasks])
item = card.checklist.order_by('position').last()
converted = checklist_item_to_subtask(item, actor=admin)
print('из чек-листа:', converted.code, converted.title, '| пункт остался:', card.checklist.filter(pk=item.pk).exists())
print('номера — общий ряд доски:', sorted(BoardCard.objects.filter(board=board).values_list('number', flat=True))[-5:])
try:
    with transaction.atomic():
        BoardCard.objects.filter(pk=converted.pk).update(column=main.columns.filter(is_done=False).first())
except IntegrityError as exc:
    print('подзадача в колонке:', type(exc).__name__, 'board_subtask_no_column' in str(exc))
try:
    create_subtask(subtasks[0], actor=admin, title='Внук')
except BoardError as exc:
    print('подзадача подзадачи:', exc)
print('счётчики колонок не изменились:', column_counts(main, admin) == before_counts)
state = build_board_state(board, main, admin, card_id=card.pk)
tile = next(item for column in state['columns'] for item in column['cards'] if item['card'].pk == card.pk)
print('плитка: ⧉', f"{tile['subtask_done']}/{tile['subtask_total']}", '| коды для окна броска:', tile['open_subtask_codes'])
print('подзадач среди плиток:', sum(
    1 for column in state['columns'] for item in column['cards'] if item['card'].parent_id
))
create_subtasks_from_list(card, actor=admin, text='\n'.join(f'Позиция {n}' for n in range(16)))
print('запросов build_board_state с открытой карточкой: 0 подзадач —', empty, '| 20 подзадач —', queries())
for child in BoardCard.objects.filter(parent=card).order_by('position'):
    complete_card(child, actor=ivanov, execution_comment='Готово')
done = Notification.objects.filter(event_type='BOARD_SUBTASKS_DONE')
print('«Все подзадачи выполнены»:', done.count(), 'для', sorted(done.values_list('recipient__username', flat=True)),
      '| карточка в работе:', Task.objects.get(board_card=card).status.code)
print('журнал карточки, SUBTASK:', BoardCardEvent.objects.filter(card=card, kind='SUBTASK').count())
other = board.sub_boards.order_by('position').last()
move_card(card, actor=admin, column=other.columns.filter(is_done=False).first())
print('перенос карточки на «%s»: подзадачи там же —' % other.name,
      set(BoardCard.objects.filter(parent=card).values_list('sub_board_id', flat=True)) == {other.pk})
