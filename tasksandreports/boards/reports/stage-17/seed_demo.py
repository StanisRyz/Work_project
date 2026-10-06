"""Demo data for stage17_e2e.py: stage 16's demo, aged, plus «Сумма».

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-17/seed_demo.py

Stage 16's seed (`../stage-16/seed_demo.py`, which runs stage 15's first:
«Запуск заказов» ZAP, `admin1`, `ivanov`, `petrova`, …, passwords = logins)
is run first. Then a number field «Сумма» with values, a card on «Цех ПиР»,
the deadlines spread out, and the cards' journal moved back in time so that there is something stuck —
the entries that put a card in its column (`CREATED`/`MOVED`/`REOPENED`) are
dated so many days ago:

| card  | column       | days in it | Сумма       | срок        |
| ----- | ------------ | ---------- | ----------- | ----------- |
| ZAP-1 | Сделать      | 5          | 1 250 000   | today + 2   |
| ZAP-2 | Сделать      | 9          | 480 000,5   | today + 10  |
| ZAP-3 | В работе     | 2          | 2 100 000   | yesterday   |
| ZAP-4 | На проверке  | 12         | 75 000      | today + 6   |
| ZAP-5 | В работе     | 4          |             | today + 4   |
| ZAP-6 | Готово       | —          | 990 000     | today + 4   |
| ZAP-7 | Сделать      | 0          |             | today + 4   |
| ZAP-8 | Сделать      | 1          | 15 000      | today + 1   |

No column has a «Застой» threshold yet: the browser check sets «Сделать» to
3 days through the column's «⋯» menu.
"""
import datetime
from pathlib import Path

from django.contrib.auth.models import User
from django.utils import timezone

from boards.models import Board, BoardCard, BoardCardEvent, BoardCardFieldValue
from boards.services import create_card, create_field, update_card

exec(Path('tasksandreports/boards/reports/stage-16/seed_demo.py').read_text())  # noqa: S102

board = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
ivanov = User.objects.get(username='ivanov')
petrova = User.objects.get(username='petrova')
amount = create_field(board, actor=admin, name='Сумма', kind='NUMBER')
today = timezone.localdate()
for number, value, due_in in (
    (1, '1 250 000', 2), (2, '480000,5', 10), (3, '2100000', -1), (4, '75000', 6), (8, '15000', 1),
):
    card = BoardCard.objects.get(board=board, number=number)
    task = card.tasks.get()
    update_card(
        card, actor=admin, title=card.title, description=card.description,
        due_date=today + datetime.timedelta(days=due_in),
        assignee_ids=list(task.assignees.values_list('user_id', flat=True)), field_values={amount.pk: value},
    )
done = BoardCard.objects.get(board=board, number=6)
# A completed card's values are not edited any more (`update_card()` refuses a
# closed task): written directly, for the demo only.
BoardCardFieldValue.objects.create(card=done, field=amount, value_number='990000')
pir = board.sub_boards.get(name='Цех ПиР')
create_card(
    pir, actor=admin, title='Нарезать заготовки для партии 12', due_date=timezone.localdate() + datetime.timedelta(days=6),
    assignee_ids=[petrova.pk], column=pir.columns.order_by('position').first(),
)


def noon(days):
    day = timezone.localdate() - datetime.timedelta(days=days)
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(11, 0)))


for number, days in ((1, 5), (2, 9), (3, 2), (4, 12), (5, 4), (8, 1)):
    BoardCardEvent.objects.filter(
        card__board=board, card__number=number, kind__in=['CREATED', 'MOVED', 'REOPENED'],
    ).update(created_at=noon(days))
print('seeded stage 17')
