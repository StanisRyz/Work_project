"""Mail today's board digests through the `locmem` backend and print them;
the plain text and the HTML of each are saved beside this file
(`digest-<username>.txt` / `.html`). The browser check runs it after
`ivanov` has subscribed:

    python manage.py shell < tasksandreports/boards/reports/stage-23/digest_letter.py

The same `boards.digest.send_digests()` that `manage.py board_digest` calls;
nothing is sent anywhere — `locmem` keeps the letters in memory.
"""
from pathlib import Path

from django.contrib.auth.models import User
from django.core import mail
from django.test.utils import override_settings

from boards.digest import send_digests

HERE = Path('tasksandreports/boards/reports/stage-23')

with override_settings(
    EMAIL_NOTIFICATIONS_ENABLED=True,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    APP_BASE_URL='https://quality.example',
):
    mail.outbox = []
    summary = send_digests()
    print('итог:', {key: value for key, value in summary.items() if key != 'errors'})
    for message in mail.outbox:
        user = User.objects.get(email=message.to[0])
        (HERE / f'digest-{user.username}.txt').write_text(
            f'Кому: {message.to[0]}\nТема: {message.subject}\n\n{message.body}', encoding='utf-8',
        )
        (HERE / f'digest-{user.username}.html').write_text(message.alternatives[0][0], encoding='utf-8')
        print(f'Кому: {message.to[0]}')
        print(f'Тема: {message.subject}')
        print()
        print(message.body)
