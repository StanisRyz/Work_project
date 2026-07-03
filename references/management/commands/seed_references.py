from django.core.management.base import BaseCommand

from references.models import ActStatus, DefectType, Operation, Priority, TaskStatus


class Command(BaseCommand):
    help = 'Create or update initial reference dictionaries.'

    def handle(self, *args, **options):
        operations = [
            ('NAVIVKA', 'Навивка', 10),
            ('OTZHIG_AFTER_NAVIVKA', 'Отжиг после навивки', 20),
            ('INTEROP_OTK', 'Межоперационный контроль ОТК', 30),
            ('BANDAZHIROVANIE', 'Бандажирование', 40),
            ('PROPITKA_SUSHKA', 'Пропитка/сушка', 50),
            ('ZACHISTKA_BEFORE_REZKA', 'Зачистка перед резкой', 60),
            ('REZKA', 'Резка', 70),
            ('SHLIFOVKA', 'Шлифовка', 80),
            ('ZACHISTKA_AFTER_REZKA', 'Зачистка после резки', 90),
            ('KALIBROVKA_AFTER_REZKA', 'Калибровка после резки', 100),
            ('FINAL_OTK', 'Выпускной контроль ОТК', 110),
        ]
        defect_types = [
            ('SIZE_DEVIATION', 'Размерное отклонение'),
            ('BANDAGE_DAMAGE', 'Повреждение бандажа'),
            ('GRINDING_DEFECT', 'Дефект после шлифовки'),
            ('INCOMPLETE_COATING', 'Неполное покрытие'),
            ('HOLE_SHIFT', 'Смещение отверстий'),
            ('INSULATION_DAMAGE', 'Повреждение изоляции'),
            ('DRAWING_MISMATCH', 'Несоответствие чертежу'),
            ('MARKING_ERROR', 'Ошибка маркировки'),
            ('DOCUMENTATION_MISSING', 'Отсутствие документации'),
            ('OTHER', 'Прочее'),
        ]
        act_statuses = [
            ('CREATED_OTK', 'Создан ОТК', 10, False),
            ('KO_REVIEW', 'На рассмотрении КО', 20, False),
            ('TO_ANALYSIS', 'На анализе ТО', 30, False),
            ('ACTIONS_ASSIGNED', 'Мероприятия назначены', 40, False),
            ('CLOSED', 'Закрыт', 50, True),
            ('CANCELLED', 'Отменён', 60, True),
        ]
        task_statuses = [
            ('OPEN', 'Открыта', 10, False),
            ('IN_PROGRESS', 'В работе', 20, False),
            ('ON_REVIEW', 'На проверке', 30, False),
            ('DONE', 'Выполнена', 40, True),
            ('OVERDUE', 'Просрочена', 50, False),
            ('CANCELLED', 'Отменена', 60, True),
        ]
        priorities = [
            ('LOW', 'Низкий', 10, 'gray'),
            ('MEDIUM', 'Средний', 20, 'blue'),
            ('HIGH', 'Высокий', 30, 'orange'),
            ('CRITICAL', 'Критический', 40, 'red'),
        ]

        for code, name, sort_order in operations:
            Operation.objects.update_or_create(
                code=code,
                defaults={'name': name, 'sort_order': sort_order, 'is_active': True},
            )
        for code, name in defect_types:
            DefectType.objects.update_or_create(
                code=code,
                defaults={'name': name, 'is_active': True},
            )
        for code, name, sort_order, is_final in act_statuses:
            ActStatus.objects.update_or_create(
                code=code,
                defaults={
                    'name': name,
                    'sort_order': sort_order,
                    'is_final': is_final,
                    'is_active': True,
                },
            )
        for code, name, sort_order, is_final in task_statuses:
            TaskStatus.objects.update_or_create(
                code=code,
                defaults={
                    'name': name,
                    'sort_order': sort_order,
                    'is_final': is_final,
                    'is_active': True,
                },
            )
        for code, name, sort_order, color in priorities:
            Priority.objects.update_or_create(
                code=code,
                defaults={
                    'name': name,
                    'sort_order': sort_order,
                    'color': color,
                    'is_active': True,
                },
            )

        self.stdout.write(
            self.style.SUCCESS(
                'Reference data ready: 11 operations, 10 defect types, '
                '6 act statuses, 6 task statuses, 4 priorities.'
            )
        )
