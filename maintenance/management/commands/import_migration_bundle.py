import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import (
    TransferError,
    describe_missing_media,
    import_bundle,
    plan_import,
)


ACCEPT_MISSING_MEDIA_PHRASE = 'ПРИНЯТЬ НЕПОЛНЫЙ ПЕРЕНОС'


class Command(BaseCommand):
    help = (
        'Импортирует проверенный миграционный пакет в пустую базу PostgreSQL. '
        'Ничего не удаляет и не перезаписывает.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--input', required=True, help='Каталог миграционного пакета.')
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Только проверки и план действий, без изменений базы и файлов.',
        )
        parser.add_argument(
            '--accept-missing-media',
            action='store_true',
            help=(
                'Осознанно принять неполный пакет, в котором отмечены отсутствующие файлы '
                'вложений. Требует дополнительного подтверждения.'
            ),
        )
        parser.add_argument(
            '--confirmation',
            default=None,
            help=(
                f'Подтверждение для --accept-missing-media. Точный текст: '
                f'"{ACCEPT_MISSING_MEDIA_PHRASE}". Без него команда спросит его в консоли.'
            ),
        )
        parser.add_argument('--json-report', help='Путь для сохранения JSON-результата импорта.')

    def handle(self, *args, **options):
        bundle_dir = options['input']
        accept_missing = options['accept_missing_media']
        try:
            if options['dry_run']:
                validation, actions = plan_import(bundle_dir, accept_missing_media=accept_missing)
                self._confirm_missing_media(validation, options, dry_run=True)
                self.stdout.write('Проверка пакета пройдена. Планируемые действия:')
                for action in actions:
                    self.stdout.write(f'  {action}')
                for warning in validation['warnings']:
                    self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))
                self._report_missing_media(validation['missing_media'])
                self._write_report(
                    options,
                    {
                        'mode': 'dry-run',
                        'status': 'ok',
                        'record_count': validation['record_count'],
                        'media_count': validation['media_count'],
                        'complete_bundle': validation['complete'],
                        'missing_media': describe_missing_media(validation['missing_media']),
                        'warnings': validation['warnings'],
                        'actions': actions,
                    },
                )
                self.stdout.write(
                    self.style.SUCCESS('Dry-run завершён: база и файловая система не изменены.')
                )
                return

            if accept_missing:
                # Confirm before a single row is written, using the validated list.
                validation, _actions = plan_import(bundle_dir, accept_missing_media=True)
                self._confirm_missing_media(validation, options, dry_run=False)

            result = import_bundle(bundle_dir, accept_missing_media=accept_missing)
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f'Загружено записей: {result["loaded"]}')
        self.stdout.write(
            'Последовательности восстановлены для моделей: '
            f'{len(result["sequences"]["models"])} '
            f'(выполнено инструкций — {result["sequences"]["statements"]}).'
        )
        self.stdout.write(f'Скопировано файлов media: {len(result["media"]["copied"])}')
        for warning in result['validation']['warnings']:
            self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))
        self._report_missing_media(result['missing_media'])

        self._write_report(
            options,
            {
                'mode': 'import',
                'status': result['status'],
                'loaded': result['loaded'],
                'sequences': result['sequences'],
                'media_copied': len(result['media']['copied']),
                'media_error': result['media']['error'],
                'complete_bundle': result['complete_bundle'],
                'missing_media': describe_missing_media(result['missing_media']),
                'warnings': result['validation']['warnings'],
                'recovery': result.get('recovery', []),
            },
        )

        if result['status'] != 'ok':
            self.stdout.write(self.style.ERROR('Импорт завершён ЧАСТИЧНО.'))
            self.stdout.write(self.style.ERROR(result['media']['error']))
            for step in result['recovery']:
                self.stdout.write(self.style.ERROR(f'  {step}'))
            raise CommandError(
                'Импорт частично успешен: база зафиксирована, файлы media не активированы '
                'полностью. Выполните восстановление по инструкции выше — целевая база '
                'пока не пригодна для работы.'
            )

        if not result['complete_bundle']:
            self.stdout.write(
                self.style.WARNING(
                    'Импортирован НЕПОЛНЫЙ пакет: часть файлов вложений отсутствует. '
                    'Перенос не может считаться полным.'
                )
            )
        self.stdout.write(
            self.style.SUCCESS(
                'Импорт завершён. Запустите verify_migration_bundle для итоговой сверки.'
            )
        )

    def _confirm_missing_media(self, validation, options, dry_run):
        missing = validation['missing_media']
        if not missing:
            return
        self.stdout.write(
            self.style.WARNING(f'Пакет неполный. Отсутствующие файлы вложений — {len(missing)}:')
        )
        for line in describe_missing_media(missing):
            self.stdout.write(self.style.WARNING(f'  {line}'))
        if dry_run:
            return
        supplied = options.get('confirmation')
        if supplied is None:
            try:
                supplied = input(f'Введите «{ACCEPT_MISSING_MEDIA_PHRASE}» для продолжения: ')
            except EOFError as exc:
                raise CommandError(
                    'Подтверждение неполного переноса не получено: команда запущена '
                    f'без консоли. Передайте --confirmation "{ACCEPT_MISSING_MEDIA_PHRASE}".'
                ) from exc
        if (supplied or '').strip() != ACCEPT_MISSING_MEDIA_PHRASE:
            raise CommandError(
                'Подтверждение неполного переноса не получено. Импорт отменён.'
            )

    def _report_missing_media(self, missing):
        if not missing:
            return
        self.stdout.write(
            self.style.WARNING(f'В пакете отмечено отсутствующих файлов — {len(missing)}:')
        )
        for line in describe_missing_media(missing):
            self.stdout.write(self.style.WARNING(f'  {line}'))

    def _write_report(self, options, payload):
        target = options.get('json_report')
        if not target:
            return
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8'
        )
        self.stdout.write(f'JSON-результат импорта сохранён: {path}')
