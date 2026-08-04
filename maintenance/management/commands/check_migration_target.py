import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError
from maintenance.preflight import run_target_preflight


class Command(BaseCommand):
    help = (
        'Проверяет пустую целевую базу PostgreSQL перед импортом миграционного пакета. '
        'Бизнес-данные не изменяет.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--previous-report',
            help='JSON-отчёт предыдущей репетиции: проверка незавершённого импорта.',
        )
        parser.add_argument('--json-report', help='Путь для сохранения JSON-отчёта проверки.')

    def handle(self, *args, **options):
        try:
            report = run_target_preflight(previous_report=options.get('previous_report'))
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        if options.get('json_report'):
            path = Path(options['json_report'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8'
            )
            self.stdout.write(f'JSON-отчёт сохранён: {path}')

        for check in report['checks']:
            if check['status'] == 'ok':
                self.stdout.write(f'  [ok]      {check["name"]}: {check["details"]}')
            elif check['status'] == 'warning':
                self.stdout.write(
                    self.style.WARNING(f'  [warning] {check["name"]}: {check["details"]}')
                )
            else:
                self.stdout.write(
                    self.style.ERROR(f'  [FAILED]  {check["name"]}: {check["details"]}')
                )

        if report['ok']:
            self.stdout.write(
                self.style.SUCCESS('Целевая база пригодна для импорта миграционного пакета.')
            )
            return

        raise CommandError(
            'Проверка целевой базы не пройдена: ' + ', '.join(report['failures']) + '.'
        )
