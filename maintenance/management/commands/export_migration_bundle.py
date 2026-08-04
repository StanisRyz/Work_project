from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError, export_bundle


class Command(BaseCommand):
    help = (
        'Экспортирует миграционный пакет (данные + media) из остановленной копии SQLite. '
        'Рабочую базу не изменяет.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--output',
            required=True,
            help='Каталог для пакета. Должен отсутствовать или быть пустым.',
        )
        parser.add_argument(
            '--source-media-root',
            help=(
                'Каталог media, из которого читаются вложения. '
                'По умолчанию — settings.MEDIA_ROOT. Укажите копию media.'
            ),
        )
        parser.add_argument(
            '--allow-missing-media',
            action='store_true',
            help=(
                'Диагностический режим: не прерывать экспорт при отсутствующих файлах вложений. '
                'Такой пакет считается неполным и не принимается обычным импортом.'
            ),
        )

    def handle(self, *args, **options):
        try:
            manifest = export_bundle(
                options['output'],
                allow_missing_media=options['allow_missing_media'],
                source_media_root=options.get('source_media_root'),
            )
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        total_records = sum(entry['count'] for entry in manifest['models'].values())
        source_media = manifest['source_media']
        self.stdout.write(f'Пакет сформирован: {options["output"]}')
        self.stdout.write(f'  версия формата — {manifest["bundle_format_version"]}')
        self.stdout.write(f'  источник — {manifest["source_vendor"]}')
        self.stdout.write(f'  записей — {total_records}')
        self.stdout.write(f'  моделей — {len(manifest["models"])}')
        self.stdout.write(f'  файлов media — {len(manifest["media_files"])}')
        self.stdout.write(
            f'  каталог media — {source_media["name"]} '
            f'(по умолчанию: {"да" if source_media["is_default_media_root"] else "нет"}, '
            f'файлов — {source_media["file_count"]}, {source_media["total_size"]} байт)'
        )
        self.stdout.write(f'  SHA-256 data.json — {manifest["data_file"]["sha256"]}')

        if manifest['missing_media']:
            self.stdout.write(
                self.style.WARNING(
                    f'Пакет НЕПОЛНЫЙ. Отсутствующие файлы вложений — '
                    f'{len(manifest["missing_media"])}:'
                )
            )
            for entry in manifest['missing_media']:
                self.stdout.write(
                    self.style.WARNING(
                        f'  ActAttachment id={entry["attachment_id"]}: {entry["path"]}'
                    )
                )
            self.stdout.write(
                self.style.WARNING(
                    'Обычный импорт такого пакета запрещён: потребуется '
                    'import_migration_bundle --accept-missing-media.'
                )
            )
        for warning in manifest['warnings']:
            self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))

        self.stdout.write(
            self.style.SUCCESS(
                'Экспорт завершён. Пакет содержит рабочие данные — не добавляйте его в Git.'
            )
        )
