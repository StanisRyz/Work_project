"""Demo data for stage25_e2e.py: stage 24's demo, and «Запуск в работу».

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-25/seed_demo.py

Stage 24's seed (`../stage-24/seed_demo.py`, which runs the older ones
first) lays out «Запуск заказов» ZAP — the ПДО board — with its fields,
stage norms, lights and `otk_master`. Here only what the browser check needs
on top: the third column of «Основная» is renamed «Запуск в работу»
(`rename_column()`, as its owner would). No entry rule and no action exist
yet: the browser check sets them up from the board's own pages. Every
notification of the set-up is marked read.
"""
from pathlib import Path

exec(Path('tasksandreports/boards/reports/stage-24/seed_demo.py').read_text())  # noqa: S102

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from boards.models import Board, BoardColumn  # noqa: E402
from boards.services import rename_column  # noqa: E402
from notifications.models import Notification  # noqa: E402

zap = Board.objects.get(code='ZAP')
owner = User.objects.get(username='admin1')
third = BoardColumn.objects.filter(sub_board__board=zap, is_done=False).order_by('sub_board__position', 'position')[2]
rename_column(third, actor=owner, name='Запуск в работу')

Notification.objects.filter(is_read=False).update(is_read=True, read_at=timezone.now())
print('columns:', [(c.pk, c.name) for c in BoardColumn.objects.filter(sub_board__board=zap).order_by('sub_board_id', 'position')])
print('seeded stage 25')
