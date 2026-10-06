"""Demo data for stage18_e2e.py: stage 17's demo plus a «Запуск заказа» checklist.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-18/seed_demo.py

Stage 17's seed (`../stage-17/seed_demo.py`, which runs 16's and 15's first:
«Запуск заказов» ZAP, `admin1` — Олег Админов, `ivanov` — Иван Иванов,
`petrova` — Мария Петрова, …, passwords = logins) is run first. Then ZAP-1
gets the pilot's checklist «Запуск заказа», five steps, the first two ticked
by Иван Иванов, its исполнитель:

| # | пункт                         | сделано |
| - | ----------------------------- | ------- |
| 1 | Согласовать спецификацию      | да      |
| 2 | Проверить наличие металла     | да      |
| 3 | Передать заказ в цех МП       | нет     |
| 4 | Запустить партию в работу     | нет     |
| 5 | Принять партию ОТК            | нет     |

Nobody follows any card yet, and no message mentions anybody: the browser
check does both through the page.
"""
from pathlib import Path

from django.contrib.auth.models import User

from boards.models import Board, BoardCard
from boards.services import add_checklist_item, toggle_checklist_item

exec(Path('tasksandreports/boards/reports/stage-17/seed_demo.py').read_text())  # noqa: S102

board = Board.objects.get(code='ZAP')
ivanov = User.objects.get(username='ivanov')
card = BoardCard.objects.get(board=board, number=1)
items = [
    add_checklist_item(card, actor=ivanov, text=text)
    for text in (
        'Согласовать спецификацию',
        'Проверить наличие металла',
        'Передать заказ в цех МП',
        'Запустить партию в работу',
        'Принять партию ОТК',
    )
]
for item in items[:2]:
    toggle_checklist_item(item, actor=ivanov)
print('seeded stage 18')
