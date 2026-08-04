from django.core.management.base import BaseCommand, CommandError

from maintenance.database_transfer import TransferError
from maintenance.target_preparation import CONFIRMATION_PHRASE, prepare_empty_target


class Command(BaseCommand):
    help = (
        'Готовит пустую тестовую базу PostgreSQL к импорту: удаляет только строки '
        'ActStatus/TaskStatus, созданные миграциями данных. По умолчанию — dry-run. '
        'Пользовательские данные, акты, задачи, уведомления и файлы не трогает.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Выполнить удаление. Без флага команда только показывает план.',
        )
        parser.add_argument(
            '--confirm',
            default='',
            help=f'Текстовое подтверждение для --execute. Точный текст: "{CONFIRMATION_PHRASE}".',
        )

    def handle(self, *args, **options):
        try:
            result = prepare_empty_target(
                execute=options['execute'], confirmation=options['confirm']
            )
        except TransferError as exc:
            raise CommandError(str(exc)) from exc

        mode = 'DRY-RUN (изменений нет)' if result['dry_run'] else 'ВЫПОЛНЕНИЕ'
        self.stdout.write(f'Режим: {mode}')

        if result['user_data']:
            self.stdout.write(self.style.ERROR('Обнаружены пользовательские данные:'))
            for entry in result['user_data']:
                self.stdout.write(
                    self.style.ERROR(
                        f'  {entry["model"]} (таблица {entry["table"]}): строк — {entry["rows"]}'
                    )
                )

        if result['seeded']:
            self.stdout.write('Справочники, заполняемые миграциями данных:')
            for entry in result['seeded']:
                self.stdout.write(
                    f'  {entry["model"]} (таблица {entry["table"]}): всего строк — {entry["rows"]}, '
                    f'разрешено к удалению — {entry["allowed_codes"]}, '
                    f'прочие коды — {entry["other_codes"]}'
                )

        if result['planned']:
            self.stdout.write('Затрагиваемые таблицы и строки:')
            for entry in result['planned']:
                self.stdout.write(
                    f'  {entry["table"]}: {entry["rows"]} строк — коды {", ".join(entry["codes"])}'
                )
        else:
            self.stdout.write('Удалять нечего: строк миграций данных нет.')

        if result['deleted']:
            self.stdout.write('Удалено:')
            for entry in result['deleted']:
                self.stdout.write(
                    f'  {entry["table"]}: {entry["rows"]} строк — коды {", ".join(entry["codes"])}'
                )

        if result['blocking']:
            for problem in result['blocking']:
                self.stdout.write(self.style.ERROR(problem))
            raise CommandError(
                'Подготовка целевой базы отклонена: используйте отдельную пустую тестовую базу.'
            )

        if result['dry_run']:
            if result['planned']:
                self.stdout.write(
                    self.style.WARNING(
                        f'Для удаления повторите с --execute --confirm "{CONFIRMATION_PHRASE}".'
                    )
                )
            self.stdout.write(
                self.style.SUCCESS('Dry-run завершён: база и файлы не изменены.')
            )
            return

        if result['ready']:
            self.stdout.write(
                self.style.SUCCESS('Целевая база готова: все переносимые таблицы пусты.')
            )
            return
        raise CommandError('Целевая база не готова: остались непустые переносимые таблицы.')
