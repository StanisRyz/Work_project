"""Demo data for stage16_e2e.py: stage 15's demo plus the pilot's fields and values.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-16/seed_demo.py

Stage 15's seed (`../stage-15/seed_demo.py`: «Запуск заказов» ZAP, `admin1`,
`ivanov`, `petrova`, …, passwords = logins) is run first; then the fields
stage 15 set up through the page — «Номер заявки», «Заказ покупателя»
(text), «Срок изготовления» (date), «Приоритет» (Высокий/red, Средний/yellow,
Низкий/gray), «Стоп» (Да/red) — and values on the cards of «Основная»:

| card  | column       | Номер заявки | Срок       | Приоритет | Стоп |
| ----- | ------------ | ------------ | ---------- | --------- | ---- |
| ZAP-1 | Сделать      | 3-1579       | 2026-11-30 | Высокий   | Да   |
| ZAP-2 | Сделать      | 3-1580       | 2026-12-15 | Средний   |      |
| ZAP-3 | В работе     | 3-1581       | 2026-11-20 | Высокий   |      |
| ZAP-4 | На проверке  | 3-1582       | 2026-11-10 | Низкий    | Да   |
| ZAP-5 | В работе     | 3-1583       | 2026-12-01 |           | Да   |
| ZAP-6 | Готово       | 3-1584       | 2026-10-30 | Высокий   |      |
| ZAP-7 | Сделать      |              |            |           |      |
| ZAP-8 | Сделать      | 3-1586       |            |           |      |
"""
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.utils import timezone

from boards.models import Board, BoardCard
from boards.services import complete_card, create_card, create_field, update_card

exec(Path('tasksandreports/boards/reports/stage-15/seed_demo.py').read_text())  # noqa: S102

board = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
order = create_field(board, actor=admin, name='Номер заявки', kind='TEXT')
create_field(board, actor=admin, name='Заказ покупателя', kind='TEXT')
deadline = create_field(board, actor=admin, name='Срок изготовления', kind='DATE')
priority = create_field(
    board, actor=admin, name='Приоритет', kind='SELECT',
    options=[('Высокий', 'red'), ('Средний', 'yellow'), ('Низкий', 'gray')],
)
stop = create_field(board, actor=admin, name='Стоп', kind='SELECT', options=[('Да', 'red')])
high, middle, low = priority.options.order_by('position')
yes = stop.options.get()
main = board.sub_boards.order_by('position').first()
columns = list(main.columns.order_by('position'))
due = timezone.localdate() + timedelta(days=4)


def values(number=None, date=None, option=None, stopped=False):
    result = {}
    if number:
        result[order.pk] = number
    if date:
        result[deadline.pk] = date
    if option:
        result[priority.pk] = option.pk
    if stopped:
        result[stop.pk] = yes.pk
    return result


for number, field_values in (
    (1, values('3-1579', '2026-11-30', high, True)),
    (2, values('3-1580', '2026-12-15', middle)),
    (3, values('3-1581', '2026-11-20', high)),
    (4, values('3-1582', '2026-11-10', low, True)),
):
    card = BoardCard.objects.get(board=board, number=number)
    task = card.tasks.get()
    update_card(
        card, actor=admin, title=card.title, description=card.description, due_date=task.due_date,
        assignee_ids=list(task.assignees.values_list('user_id', flat=True)), field_values=field_values,
    )
create_card(
    main, actor=admin, title='Подготовить сопроводительные документы', due_date=due,
    assignee_ids=[ivanov.pk], column=columns[1], field_values=values('3-1583', '2026-12-01', stopped=True),
)
done = create_card(
    main, actor=admin, title='Закупить металл', due_date=due,
    assignee_ids=[ivanov.pk], column=columns[0], field_values=values('3-1584', '2026-10-30', high),
)
complete_card(done, actor=ivanov, execution_comment='Металл на складе.')
create_card(main, actor=admin, title='Согласовать упаковку', due_date=due, assignee_ids=[ivanov.pk], column=columns[0])
create_card(
    main, actor=admin, title='Заказать оснастку', due_date=due, assignee_ids=[ivanov.pk], column=columns[0],
    field_values=values('3-1586'),
)
print('seeded stage 16')
