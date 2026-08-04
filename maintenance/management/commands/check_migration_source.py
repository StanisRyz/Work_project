import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError
from maintenance.preflight import run_source_preflight


class Command(BaseCommand):
    help = (
        'Проверяет исходную копию SQLite перед экспортом миграционного пакета. '
        'Ничего не изменяет.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-media-root',
            help='Каталог media источника. По умолчанию — settings.MEDIA_ROOT.',
        )
        parser.add_argument(
            '--allow-default-database',
            action='store_true',
            help=(
                'Разрешить проверку на рабочем db.sqlite3. Без этого флага требуется '
                'отдельная остановленная копия базы.'
            ),
        )
        parser.add_argument('--json-report', help='Путь для сохранения JSON-отчёта проверки.')

    def handle(self, *args, **options):
        try:
            report = run_source_preflight(
                source_media_root=options.get('source_media_root'),
                allow_default_database=options['allow_default_database'],
            )
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
                self.style.SUCCESS('Источник пригоден для экспорта миграционного пакета.')
            )
            return

        if any(check['name'] == 'database_copy' for check in report['checks'] if check['status'] == 'failed'):
            self.stdout.write(
                self.style.ERROR(
                    'Сначала остановите приложение, скопируйте db.sqlite3 и media, '
                    'затем укажите SQLITE_DB_PATH на копию и --source-media-root на копию media.'
                )
            )
        raise CommandError(
            'Проверка источника не пройдена: ' + ', '.join(report['failures']) + '.'
        )
