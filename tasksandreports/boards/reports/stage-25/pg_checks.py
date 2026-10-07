"""Stage 25 on PostgreSQL: «Правила при входе», «Передать дальше», «📌»,
«Списком», on stage 25's demo.

    python manage.py shell < tasksandreports/boards/reports/stage-25/pg_checks.py

Prints what it finds; every line is a fact checked on the real database —
the entry rules applied on a move and on creation, one journal entry, the
checklist limit, an action run in one transaction and rolled back whole when
a step fails, the constraints of the new tables, pinning and the list.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction

from boards.models import (
    BoardAction,
    BoardActionAssignee,
    BoardCard,
    BoardCardChecklistItem,
    BoardCardComment,
    BoardCardEvent,
    BoardCardFieldValue,
    BoardColumn,
    BoardColumnFieldRule,
    Board,
)
from boards.selectors import describe_card_event
from boards.services import (
    BoardError,
    add_template_item,
    create_action,
    create_cards_from_list,
    create_column,
    delete_column,
    move_card,
    run_board_action,
    set_card_pinned,
    set_column_field_rules,
    set_column_followers,
)
from notifications.models import Notification
from tasks.models import Task

zap = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
petrova = User.objects.get(username='petrova')
launch = BoardColumn.objects.get(sub_board__board=zap, name='Запуск в работу')
todo = BoardColumn.objects.get(sub_board__board=zap, sub_board__position=1, name='Сделать')
priority = zap.fields.get(name='Приоритет')
high = priority.options.get(label='Высокий')


def checklist(card):
    return list(BoardCardChecklistItem.objects.filter(card=card).order_by('position').values_list('text', flat=True))


def people(card):
    return sorted(Task.objects.get(board_card=card).assignees.values_list('user__username', flat=True))


for line in ('Проверить КД', 'Заказать материал', 'Выдать задание в цех'):
    add_template_item(launch, actor=admin, text=line)
print('значение поля:', set_column_field_rules(launch, actor=admin, values={priority.pk: (str(high.pk), False)}))
print('то же значение ещё раз:', set_column_field_rules(launch, actor=admin, values={priority.pk: (str(high.pk), False)}))
set_column_followers(launch, actor=admin, user_ids=[petrova.pk])

card = BoardCard.objects.get(board=zap, number=7)
move_card(card, actor=ivanov, column=launch)
entries = BoardCardEvent.objects.filter(card=card, kind='EDITED', details__has_key='by_column')
print('ZAP-7 вошла: чек-лист', checklist(card)[-3:], '| приоритет',
      BoardCardFieldValue.objects.get(card=card, field=priority).option.label,
      '| следит petrova:', card.subscriptions.filter(user=petrova).exists())
print('записей «Правила колонки»:', entries.count(), '|', describe_card_event(entries.get()))
before = BoardCardEvent.objects.filter(card=card).count()
other = BoardCard.objects.filter(board=zap, column=launch).exclude(pk=card.pk).first()
move_card(card, actor=ivanov, column=launch, before_card_id=other.pk)
print('перестановка внутри колонки — новых записей:', BoardCardEvent.objects.filter(card=card).count() - before)

action = create_action(
    zap, actor=admin, name='Передать в ПДО', target_column=launch, assignee_mode='REPLACE',
    assignee_ids=[petrova.pk], message_template='Передано в ПДО: {код} → «{колонка}»', comment_required=True,
)
try:
    run_board_action(BoardCard.objects.get(board=zap, number=8), action, actor=ivanov, comment='')
except BoardError as error:
    print('без комментария:', error)
zap8 = BoardCard.objects.get(board=zap, number=8)
with mock.patch('notifications.services.notify_board_card_comment', side_effect=RuntimeError('сбой')):
    try:
        run_board_action(zap8, action, actor=ivanov, comment='Срочно')
    except RuntimeError:
        zap8.refresh_from_db()
        print('сбой шага — откат: колонка', zap8.column.name, '| исполнители', people(zap8),
              '| сообщений', BoardCardComment.objects.filter(card=zap8).count(),
              '| MOVED', BoardCardEvent.objects.filter(card=zap8, kind='MOVED').count())
run_board_action(zap8, action, actor=ivanov, comment='Срочно')
zap8.refresh_from_db()
print('после нажатия: колонка', zap8.column.name, '| исполнители', people(zap8),
      '| чек-лист', checklist(zap8)[-3:])
print('сообщение:', BoardCardComment.objects.get(card=zap8).text.replace('\n', ' / '))
print('«Назначена карточка ZAP-8» у petrova:', Notification.objects.filter(
    recipient=petrova, event_type='BOARD_TASK_ASSIGNED', related_task__board_card=zap8).count())
moved = BoardCardEvent.objects.get(card=zap8, kind='MOVED')
print('журнал:', describe_card_event(moved))
# A column with no open card that an action leads to: only the action holds it.
spare = create_column(launch.sub_board, actor=admin, name='Приёмка ПДО')
create_action(zap, actor=admin, name='На приёмку', target_column=spare)
try:
    delete_column(spare, actor=admin)
except BoardError as error:
    print('удалить пустую колонку, куда ведёт действие:', error)

for label, statement in (
    ('правило поля дважды', lambda: BoardColumnFieldRule.objects.create(column=launch, field=priority, value='1')),
    ('исполнитель действия дважды', lambda: BoardActionAssignee.objects.create(action=action, user=petrova)),
    ('неизвестный режим', lambda: BoardAction.objects.filter(pk=action.pk).update(assignee_mode='NEVER')),
):
    try:
        with transaction.atomic():
            statement()
        print(label, '— принято (ошибка!)')
    except IntegrityError:
        print(label, '— отказ базы')

first_todo = list(BoardCard.objects.filter(
    board=zap, column=todo, parent__isnull=True, tasks__status__code='IN_PROGRESS',
).order_by('-is_pinned', 'position'))
last = first_todo[-1]
set_card_pinned(last, actor=ivanov, pinned=True)
print('закреплена первой:', last.code, '→', BoardCard.objects.filter(
    column=todo, tasks__status__code='IN_PROGRESS').order_by('-is_pinned', 'position').first().code)

cards = create_cards_from_list(todo.sub_board, actor=ivanov, text='Заказ А\n\nЗаказ Б\nЗаказ В', due_date=zap8.tasks.get().due_date, column=todo.pk)
print('списком:', [c.code for c in cards], '| исполнитель', {tuple(people(c)) for c in cards})
try:
    create_cards_from_list(todo.sub_board, actor=ivanov, text='\n'.join(['x'] * 31), due_date=zap8.tasks.get().due_date)
except BoardError as error:
    print('31 строка:', error)
