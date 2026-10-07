"""Demo data for stage22_e2e.py: stage 21's demo plus a second board.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-22/seed_demo.py

Stage 21's seed (`../stage-21/seed_demo.py`, which runs the older ones
first) lays out «Запуск заказов» ZAP: ZAP-1 «Согласовать спецификацию» is
`ivanov`'s, the field «Сумма» is a number. Here:
- the board «Снабжение» (code СНБ), owned by `admin1`, members `worker1` and
  `ivanov` — so ZAP-1's исполнитель reads it and his «можно начинать» names
  the code;
- СНБ-1 «Закупить лист 2 мм» on `worker1`, in «Сделать»;
- «Сумма» values on a few open ZAP cards, so the column sums read something.
No link, no column subscription and no summed field yet: the browser check
makes them. Every notification of the set-up is marked read.
"""
from pathlib import Path

exec(Path('tasksandreports/boards/reports/stage-21/seed_demo.py').read_text())  # noqa: S102

import datetime  # noqa: E402

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from boards.models import Board, BoardCard, BoardField  # noqa: E402
from boards.services import create_board, create_card, update_card  # noqa: E402
from notifications.models import Notification  # noqa: E402

admin = User.objects.get(username='admin1')
worker1 = User.objects.get(username='worker1')
ivanov = User.objects.get(username='ivanov')
snb = create_board(
    name='Снабжение', code='СНБ', owner=admin, actor=admin, member_ids=[worker1.pk, ivanov.pk],
)
create_card(
    snb.sub_boards.get(), actor=admin, title='Закупить лист 2 мм',
    due_date=timezone.localdate() + datetime.timedelta(days=3), assignee_ids=[worker1.pk],
)

zap = Board.objects.get(code='ZAP')
amount = BoardField.objects.get(board=zap, name='Сумма')
for number, value in ((1, '1250000'), (2, '800000'), (3, '2150000'), (5, '400000'), (7, '75000')):
    card = BoardCard.objects.get(board=zap, number=number)
    task = card.tasks.get()
    update_card(
        card, actor=admin, title=card.title, description=card.description, due_date=task.due_date,
        assignee_ids=list(task.assignees.values_list('user_id', flat=True)),
        field_values={amount.pk: value},
    )

Notification.objects.filter(is_read=False).update(is_read=True, read_at=timezone.now())
print('seeded stage 22')
