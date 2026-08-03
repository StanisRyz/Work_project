from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError, import_bundle, plan_import


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

    def handle(self, *args, **options):
        bundle_dir = options['input']
        try:
            if options['dry_run']:
                validation, actions = plan_import(bundle_dir)
                self.stdout.write('Проверка пакета пройдена. Планируемые действия:')
                for action in actions:
                    self.stdout.write(f'  {action}')
                for warning in validation['warnings']:
                    self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))
                if validation['missing_media']:
                    self.stdout.write(
                        self.style.WARNING(
                            f'В пакете отмечено отсутствующих файлов — {len(validation["missing_media"])}.'
                        )
                    )
                self.stdout.write(
                    self.style.SUCCESS('Dry-run завершён: база и файловая система не изменены.')
                )
                return

            result = import_bundle(bundle_dir)
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(f'Загружено записей: {result["loaded"]}')
        self.stdout.write(
            'Последовательности восстановлены для моделей: '
            f'{len(result["sequences"]["models"])} '
            f'(выполнено инструкций — {result["sequences"]["statements"]}).'
        )
        self.stdout.write(f'Скопировано файлов media: {len(result["media"]["copied"])}')
        numbers = result['act_number_sequences']
        self.stdout.write(
            'ActNumberSequence: создано — '
            f'{len(numbers["created"])}, поднято — {len(numbers["raised"])}, '
            f'без изменений — {len(numbers["unchanged"])}.'
        )
        for warning in result['validation']['warnings']:
            self.stdout.write(self.style.WARNING(f'Предупреждение: {warning}'))
        self.stdout.write(
            self.style.SUCCESS(
                'Импорт завершён. Запустите verify_migration_bundle для итоговой сверки.'
            )
        )
