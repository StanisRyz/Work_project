"""Demo data for stage21_e2e.py: stage 20's demo plus the history of
deadline moves the «Отклонения» report reads.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-21/seed_demo.py

Stage 20's seed (`../stage-20/seed_demo.py`, which runs 19's … 15's first)
lays out the board «Запуск заказов» ZAP. Two of the older seeds move a
card's срок with `update_card()` — today a move needs a reason — so they run
here with «Загрузка цеха» supplied; those set-up moves are then removed, and
every card's «исходный срок» is the срок it has after the set-up, so the
demo starts with no history at all.

Then the history the report reads, written through `update_card()` and
back-dated (`changed_at`) to spread over the last weeks:
- ZAP-2: «Ждём материал (снабжение)» +3 days, «Нет КД / ждём конструктора»
  +4 days;
- ZAP-4: «Ждём материал (снабжение)» +2 days, with a comment;
- ZAP-5: «Брак» +5 days;
- a new card on the sub-board «Цех ПиР», «Нарезать пластины по заказу
  3-1590»: «Оборудование» +6 days;
- OTG-1 (another board, «Отгрузка»): «Ждём оплату» +7 days — it is not in
  ZAP's report.
ZAP-1 is moved twice by the browser check itself. `petrova` follows ZAP-1.
Every notification of the set-up is marked read, so the bells start clean.
ZAP-3 stays overdue (yesterday) and ZAP-8 due on the next working day — the
two the reminders command speaks about.
"""
import datetime
from pathlib import Path

from django.utils import timezone

import boards.services as board_services
from references.models import DeviationReason

_update_card = board_services.update_card
_capacity = DeviationReason.objects.get(code='CAPACITY').pk


def _set_up_update(card, **kwargs):
    kwargs.setdefault('due_reason_id', _capacity)
    return _update_card(card, **kwargs)


board_services.update_card = _set_up_update
try:
    exec(Path('tasksandreports/boards/reports/stage-20/seed_demo.py').read_text())  # noqa: S102
finally:
    board_services.update_card = _update_card

from django.contrib.auth.models import User  # noqa: E402

from boards.models import Board, BoardCard, BoardCardDueChange  # noqa: E402
from boards.services import create_card, toggle_card_subscription, update_card  # noqa: E402
from ecosystem.workdays import add_working_days  # noqa: E402

BoardCardDueChange.objects.all().delete()
for card in BoardCard.objects.all():
    card.original_due_date = card.tasks.get().due_date
    card.save(update_fields=['original_due_date'])

board = Board.objects.get(code='ZAP')
admin = User.objects.get(username='admin1')
petrova = User.objects.get(username='petrova')
today = timezone.localdate()
now = timezone.now()


def move(number, days, reason, comment='', *, days_ago, code='ZAP'):
    card = BoardCard.objects.get(board__code=code, number=number)
    task = card.tasks.get()
    update_card(
        card, actor=admin, title=card.title, description=card.description,
        due_date=task.due_date + datetime.timedelta(days=days),
        assignee_ids=list(task.assignees.values_list('user_id', flat=True)),
        due_reason_id=DeviationReason.objects.get(code=reason).pk, due_comment=comment,
    )
    BoardCardDueChange.objects.filter(pk=BoardCardDueChange.objects.latest('pk').pk).update(
        changed_at=now - datetime.timedelta(days=days_ago),
    )


move(2, 3, 'MATERIAL', 'Лист 2 мм со склада только в пятницу.', days_ago=12)
move(2, 4, 'DESIGN', days_ago=5)
move(4, 2, 'MATERIAL', 'Крепёж М8 едет от поставщика.', days_ago=8)
move(5, 5, 'DEFECT', 'Повтор после брака на навивке.', days_ago=3)
pir = board.sub_boards.get(name='Цех ПиР')
plates = create_card(
    pir, actor=admin, title='Нарезать пластины по заказу 3-1590', due_date=today + datetime.timedelta(days=4),
    assignee_ids=[User.objects.get(username='ivanov').pk],
)
move(plates.number, 6, 'EQUIPMENT', 'Гильотина в ремонте до среды.', days_ago=6)
move(1, 7, 'PAYMENT', 'Ждём 50 % предоплаты.', days_ago=10, code='OTG')
toggle_card_subscription(BoardCard.objects.get(board=board, number=1), actor=petrova, subscribe=True)

# The two the reminders speak about: ZAP-3 overdue, ZAP-8 on the next working day.
BoardCard.objects.get(board=board, number=3).tasks.update(due_date=today - datetime.timedelta(days=1))
BoardCard.objects.get(board=board, number=8).tasks.update(due_date=add_working_days(today, 1))
# The bells start clean: what the set-up said is read already.
from notifications.models import Notification  # noqa: E402

Notification.objects.filter(is_read=False).update(is_read=True, read_at=now)
print('seeded stage 21')
