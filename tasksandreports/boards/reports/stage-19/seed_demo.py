"""Demo data for stage19_e2e.py: stage 18's demo plus an older task attachment.

Run on an empty database:

    python manage.py migrate
    python manage.py shell < tasksandreports/boards/reports/stage-19/seed_demo.py

Stage 18's seed (`../stage-18/seed_demo.py`, which runs 17's, 16's and 15's
first: «Запуск заказов» ZAP, `admin1` — Олег Админов, `ivanov` — Иван
Иванов, `petrova` — Мария Петрова, …, passwords = logins; ZAP-1 with its
checklist «Запуск заказа») is run first. Then the task of ZAP-1 gets one
`TaskAttachment` the way it was added before stage 19 — «спецификация-ZAP-1.pdf»
by Иван Иванов, two days ago — written directly, since a `BOARD` task takes
no new attachment now. The browser check sends every file of «Чат» itself.
"""
import datetime
from pathlib import Path

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.utils import timezone

from boards.models import BoardCard
from tasks.models import Task, TaskAttachment

exec(Path('tasksandreports/boards/reports/stage-18/seed_demo.py').read_text())  # noqa: S102

ivanov = User.objects.get(username='ivanov')
card = BoardCard.objects.get(board__code='ZAP', number=1)
task = Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)
content = b'%PDF-1.4\n% spec of ZAP-1\n'
attachment = TaskAttachment(
    task=task, uploaded_by=ivanov, original_name='спецификация-ZAP-1.pdf',
    file_size=len(content), content_type='application/pdf',
)
attachment.file.save('spec.pdf', ContentFile(content), save=False)
attachment.save()
TaskAttachment.objects.filter(pk=attachment.pk).update(created_at=timezone.now() - datetime.timedelta(days=2))
print('seeded stage 19')
