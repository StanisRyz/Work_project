import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError
from maintenance.smoke_checks import run_smoke_checks


class Command(BaseCommand):
    help = (
        'Выполняет smoke-проверки перенесённой базы PostgreSQL: read-only обход реальных '
        'данных и полный сценарий записи внутри транзакции с обязательным откатом. '
        'Email не отправляется.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--read-only',
            action='store_true',
            help='Выполнить только read-only проверки, без сценария записи.',
        )
        parser.add_argument('--json-report', help='Путь для сохранения JSON-отчёта проверок.')

    def handle(self, *args, **options):
        try:
            report = run_smoke_checks(include_write=not options['read_only'])
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        if options.get('json_report'):
            path = Path(options['json_report'])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8'
            )
            self.stdout.write(f'JSON-отчёт сохранён: {path}')

        for suite_name, suite in (('read-only', report['read']), ('write', report['write'])):
            if not suite['checks']:
                continue
            self.stdout.write(f'Проверки {suite_name}:')
            for check in suite['checks']:
                if check['status'] == 'ok':
                    self.stdout.write(f'  [ok]     {check["name"]}: {check["details"]}')
                else:
                    self.stdout.write(
                        self.style.ERROR(f'  [FAILED] {check["name"]}: {check["details"]}')
                    )

        self.stdout.write(f'Длительность: {report["duration_seconds"]} с')

        if report['ok']:
            self.stdout.write(
                self.style.SUCCESS(
                    'Smoke-проверки пройдены. Тестовые данные записи откачены полностью.'
                )
            )
            return

        failed = list(report['read']['failures']) + list(report['write'].get('failures') or [])
        raise CommandError('Smoke-проверки не пройдены: ' + ', '.join(failed) + '.')
