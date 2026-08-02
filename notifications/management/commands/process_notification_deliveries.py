from django.core.management.base import BaseCommand, CommandError

from notifications.email_delivery import process_pending_deliveries


class Command(BaseCommand):
    help = 'Обрабатывает очередь email-доставок уведомлений.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size',
            type=int,
            default=None,
            help='Максимум доставок за один запуск (по умолчанию EMAIL_NOTIFICATION_BATCH_SIZE, обычно 100).',
        )

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        if batch_size is not None and batch_size < 1:
            raise CommandError('--batch-size должен быть положительным числом.')
        summary = process_pending_deliveries(batch_size=batch_size)
        self.stdout.write(
            self.style.SUCCESS(
                'Обработка завершена: '
                f"обработано — {summary['processed']}, "
                f"отправлено — {summary['sent']}, "
                f"ожидает повтора — {summary['pending']}, "
                f"ошибок — {summary['failed']}, "
                f"пропущено — {summary['skipped']}."
            )
        )
