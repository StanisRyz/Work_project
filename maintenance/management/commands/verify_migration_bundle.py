import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError, verify_against_bundle


class Command(BaseCommand):
    help = (
        'Сверяет текущую базу и MEDIA_ROOT с миграционным пакетом. '
        'Возвращает ненулевой код при любом расхождении.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--input', required=True, help='Каталог миграционного пакета.')
        parser.add_argument('--report', help='Путь для сохранения JSON-отчёта.')

    def handle(self, *args, **options):
        try:
            report = verify_against_bundle(options['input'])
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        if options['report']:
            report_path = Path(options['report'])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
                encoding='utf-8',
            )
            self.stdout.write(f'Отчёт сохранён: {report_path}')

        matched_models = sum(1 for entry in report['models'].values() if entry['matches'])
        self.stdout.write(f'Проверено моделей: {matched_models}/{len(report["models"])}')
        self.stdout.write(
            f'Проверено файлов media: {report["media"]["checked"]}/{report["media"]["expected"]}'
        )

        if report['ok']:
            self.stdout.write(
                self.style.SUCCESS(
                    'Сверка пройдена: данные, связи и файлы соответствуют пакету.'
                )
            )
            return

        self.stdout.write(self.style.ERROR(f'Найдено расхождений: {len(report["differences"])}'))
        for difference in report['differences']:
            self.stdout.write(self.style.ERROR(f'  {difference}'))
        raise CommandError('Сверка не пройдена: данные не соответствуют миграционному пакету.')
