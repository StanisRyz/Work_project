import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import (
    TransferError,
    describe_missing_media,
    validate_bundle,
    verify_against_bundle,
)


class Command(BaseCommand):
    help = (
        'Сверяет текущую базу и MEDIA_ROOT с миграционным пакетом. '
        'Возвращает ненулевой код при любом расхождении.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--input', required=True, help='Каталог миграционного пакета.')
        parser.add_argument('--report', help='Путь для сохранения JSON-отчёта.')
        parser.add_argument(
            '--validate-only',
            action='store_true',
            help=(
                'Только проверка целостности пакета (структура, контрольные суммы, '
                'пересчёт статистики из data.json) без обращения к данным целевой базы.'
            ),
        )
        parser.add_argument(
            '--allow-missing-media',
            action='store_true',
            help=(
                'Специальный режим проверки: не считать отмеченные в пакете отсутствующие '
                'файлы расхождением. Перенос при этом остаётся неполным.'
            ),
        )

    def handle(self, *args, **options):
        try:
            if options['validate_only']:
                self._validate_only(options)
                return
            report = verify_against_bundle(
                options['input'], allow_missing_media=options['allow_missing_media']
            )
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        self._save_report(options.get('report'), report)

        matched_models = sum(1 for entry in report['models'].values() if entry['matches'])
        self.stdout.write(f'Проверено моделей: {matched_models}/{len(report["models"])}')
        self.stdout.write(
            f'Проверено файлов media: {report["media"]["checked"]}/{report["media"]["expected"]}'
        )
        if report['missing_media']:
            self.stdout.write(
                self.style.WARNING(
                    f'Отсутствующие файлы вложений — {len(report["missing_media"])}:'
                )
            )
            for line in report['missing_media']:
                self.stdout.write(self.style.WARNING(f'  {line}'))
        for warning in report['warnings']:
            self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))

        if report['ok']:
            if not report['complete_transfer']:
                self.stdout.write(
                    self.style.WARNING(
                        'Расхождений нет, но перенос НЕПОЛНЫЙ: часть файлов вложений '
                        'отсутствует и это было разрешено явно.'
                    )
                )
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

    def _validate_only(self, options):
        validation = validate_bundle(options['input'])
        report = {
            'mode': 'validate-only',
            'record_count': validation['record_count'],
            'media_count': validation['media_count'],
            'complete': validation['complete'],
            'missing_media': describe_missing_media(validation['missing_media']),
            'warnings': validation['warnings'],
            'models': {
                label: entry for label, entry in validation['recomputed_models'].items()
            },
            'ok': True,
        }
        self._save_report(options.get('report'), report)
        self.stdout.write(f'Записей в пакете: {validation["record_count"]}')
        self.stdout.write(f'Файлов media в пакете: {validation["media_count"]}')
        for warning in validation['warnings']:
            self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))
        if not validation['complete']:
            self.stdout.write(
                self.style.WARNING('Пакет НЕПОЛНЫЙ: отмечены отсутствующие файлы вложений.')
            )
            for line in describe_missing_media(validation['missing_media']):
                self.stdout.write(self.style.WARNING(f'  {line}'))
        self.stdout.write(
            self.style.SUCCESS('Проверка пакета пройдена: структура и контрольные суммы верны.')
        )

    def _save_report(self, target, report):
        if not target:
            return
        report_path = Path(target)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False),
            encoding='utf-8',
        )
        self.stdout.write(f'Отчёт сохранён: {report_path}')
