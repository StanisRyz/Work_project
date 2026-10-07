"""Demo data for stage23_e2e.py: stage 22's demo plus stage norms and a
journal dated so that the lights show all three colours.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-23/seed_demo.py

Stage 22's seed (`../stage-22/seed_demo.py`, which runs the older ones
first) lays out «Запуск заказов» ZAP with its cards. Here, on its «Основная»:
- «Норматив этапа» 3 р.д. on «Сделать» and 5 р.д. on «В работе»
  («На проверке» keeps none — the browser check sets it from the menu);
- the open cards of «Сделать» and «В работе» get the day they entered their
  column moved back (the journal's latest `CREATED`/`MOVED`/`REOPENED`
  entry): the first of each column today (green), the second exactly the
  norm ago (yellow: the plan is today), the rest two working days past
  the plan (red);
- a new card ZAP-«Изготовить кронштейн» walked through «Сделать» → «В
  работе» → «На проверке» with dated moves, so its «Этапы» read a path;
- «Проверить чертёж» completed after a stay in «Сделать» and «В работе»,
  so the report's «Этапы» has exits;
- `admin1` and `ivanov` get the addresses admin1@example.com and
  ivanov@example.com for the digest.
Every notification of the set-up is marked read.
"""
from pathlib import Path

exec(Path('tasksandreports/boards/reports/stage-22/seed_demo.py').read_text())  # noqa: S102

import datetime  # noqa: E402

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from boards.models import Board, BoardCardEvent, BoardColumn  # noqa: E402
from boards.services import complete_card, create_card, move_card, set_column_norm  # noqa: E402
from notifications.models import Notification  # noqa: E402
from tasks.models import Task  # noqa: E402

STAGE_KINDS = ('CREATED', 'MOVED', 'REOPENED')
admin = User.objects.get(username='admin1')
admin.email = 'admin1@example.com'
admin.save(update_fields=['email'])
ivanov = User.objects.get(username='ivanov')
ivanov.email = 'ivanov@example.com'
ivanov.save(update_fields=['email'])
zap = Board.objects.get(code='ZAP')
main = zap.sub_boards.order_by('position').first()
todo = BoardColumn.objects.get(sub_board=main, name='Сделать')
work = BoardColumn.objects.get(sub_board=main, name='В работе')
review = BoardColumn.objects.get(sub_board=main, name='На проверке')
today = timezone.localdate()


def back(day, working_days):
    """`working_days` working days before `day` (Monday–Friday)."""
    while working_days > 0:
        day -= datetime.timedelta(days=1)
        if day.weekday() < 5:
            working_days -= 1
    return day


def at(day, hour=10):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, 0)))


def date_entry(entry, day, hour=10):
    BoardCardEvent.objects.filter(pk=entry.pk).update(created_at=at(day, hour))


set_column_norm(todo, actor=admin, days=3)
set_column_norm(work, actor=admin, days=5)
todo.refresh_from_db()
work.refresh_from_db()

due = today + datetime.timedelta(days=14)
bracket = create_card(main, actor=admin, title='Изготовить кронштейн', due_date=due, assignee_ids=[ivanov.pk])
move_card(bracket, actor=admin, column=work)
move_card(bracket, actor=admin, column=review)
created, first, second = bracket.events.filter(kind__in=STAGE_KINDS).order_by('created_at', 'pk')
date_entry(created, back(today, 12))
date_entry(first, back(today, 7))   # 5 р.д. in «Сделать» at a norm of 3: +2
date_entry(second, back(today, 1))  # 6 р.д. in «В работе» at a norm of 5: +1

drawing = create_card(main, actor=admin, title='Проверить чертёж', due_date=due, assignee_ids=[ivanov.pk])
move_card(drawing, actor=admin, column=work)
complete_card(drawing, actor=ivanov, execution_comment='Чертёж проверен')
created, moved = drawing.events.filter(kind__in=STAGE_KINDS).order_by('created_at', 'pk')
done = drawing.events.get(kind='COMPLETED')
date_entry(created, back(today, 9))
date_entry(moved, back(today, 7))   # 2 р.д. in «Сделать»: within 3
date_entry(done, back(today, 2))    # 5 р.д. in «В работе»: within 5
Task.objects.filter(board_card=drawing).update(completed_at=at(back(today, 2)))

for column in (todo, work):
    tasks = (
        Task.objects.filter(board_card__column=column, board_card__parent__isnull=True, status__code='IN_PROGRESS')
        .select_related('board_card').order_by('board_card__position', 'pk')
    )
    for index, task in enumerate(tasks):
        if task.board_card_id == bracket.pk:
            continue
        entered = (today, back(today, column.norm_working_days))[index] if index < 2 else back(
            today, column.norm_working_days + 2,
        )
        latest = task.board_card.events.filter(kind__in=STAGE_KINDS).order_by('-created_at', '-pk').first()
        if latest is not None:
            date_entry(latest, entered, hour=9)
        print(f'  {task.board_card.code} в «{column.name}»: вошла {entered:%d.%m}')

Notification.objects.filter(is_read=False).update(is_read=True, read_at=timezone.now())
print('seeded stage 23')
