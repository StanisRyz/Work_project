"""Mail the boards' digests due today (`boards/digest.py`).

Run once a day in the morning (Task Scheduler / cron), beside
`board_due_reminders`. `DAILY` subscriptions get a letter on working days,
`WEEKLY` ones on Mondays; a second run the same day sends nothing
(`BoardDigestSubscription.last_sent_on`). Refuses to run while
`EMAIL_NOTIFICATIONS_ENABLED=false`. Prints the counts and, per failure, the
error scrubbed by `sanitize_error()`; the log carries counts only.
"""

import datetime

from django.core.management.base import BaseCommand, CommandError

from boards.digest import DigestDisabled, send_digests


class Command(BaseCommand):
    help = 'Отправляет дайджесты досок на почту: ежедневные — по рабочим дням, еженедельные — по понедельникам.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--date',
            help='Дата рассылки ГГГГ-ММ-ДД (по умолчанию — сегодня). Для проверки и повторной отправки за день.',
        )

    def handle(self, *args, **options):
        today = None
        if options.get('date'):
            try:
                today = datetime.date.fromisoformat(options['date'])
            except ValueError as exc:
                raise CommandError('Дата — в виде ГГГГ-ММ-ДД.') from exc
        try:
            summary = send_digests(today)
        except DigestDisabled as exc:
            raise CommandError(
                'EMAIL_NOTIFICATIONS_ENABLED=false: отправка писем отключена. '
                'Включите почтовую конфигурацию перед рассылкой.'
            ) from exc
        for error in summary['errors']:
            self.stderr.write(f'Не удалось отправить дайджест: {error}')
        self.stdout.write(
            'Дайджесты: '
            f"отправлено — {summary['sent']}, "
            f"пустых (не отправлены) — {summary['empty']}, "
            f"без email — {summary['skipped_no_email']}, "
            f"неактивных — {summary['skipped_inactive']}, "
            f"ошибок — {summary['failed']}."
        )
        if summary['failed']:
            raise CommandError(f"Не доставлено дайджестов: {summary['failed']}.")
