"""Demo data for stage24_e2e.py: stage 23's demo, as it is.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-24/seed_demo.py

Stage 23's seed (`../stage-23/seed_demo.py`, which runs the older ones
first) lays out «Запуск заказов» ZAP — the ПДО board — with its fields,
stage norms and lights. Here only what the browser check needs on top:
- `otk_master` (Мастер ОТК, role ОТК, department ОТК), **not** a member of
  ZAP, with an address, who files the request;
- addresses for the handlers.
«Приём заявок» stays off: the browser check turns it on from the board's
«⋯». Every notification of the set-up is marked read.
"""
from pathlib import Path

exec(Path('tasksandreports/boards/reports/stage-23/seed_demo.py').read_text())  # noqa: S102

from django.contrib.auth.models import User  # noqa: E402
from django.utils import timezone  # noqa: E402

from accounts.models import Department, UserProfile  # noqa: E402
from notifications.models import Notification  # noqa: E402

master = User.objects.create_user(
    username='otk_master', password='otk_master', first_name='Ольга', last_name='Мастерова',
    email='otk_master@example.com',
)
profile = master.userprofile
profile.role = UserProfile.Role.OTK
profile.department = Department.objects.filter(code='OTK').first() or profile.department
profile.save()
for username in ('admin1', 'ivanov', 'petrova'):
    User.objects.filter(username=username, email='').update(email=f'{username}@example.com')

Notification.objects.filter(is_read=False).update(is_read=True, read_at=timezone.now())
zap = __import__('boards.models', fromlist=['Board']).Board.objects.get(code='ZAP')
print('ZAP owner:', zap.owner.username, '| members:', sorted(m.user.username for m in zap.members.all()))
print('fields:', [(f.pk, f.name, f.kind) for f in zap.fields.all()])
print('seeded stage 24')
