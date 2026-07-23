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
            ('OPERATIONAL_CONTROL', 'Операционный контроль', 120),
            ('FINAL_CONTROL', 'Выпускной контроль', 130),
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
            ('OTHER', 'Другое'),
            ('SIZE_NONCONFORMITY', 'Несоответствие размеров'),
            ('DEFORMATION', 'Деформация'),
            ('ASYMMETRIC_CUT', 'Несимметричный рез'),
            ('OBLIQUE_CUT', 'Косой рез'),
            ('GRINDING_SIZE_DEVIATION', 'Отклонение размеров при шлифовании'),
            ('END_FACE_DELAMINATION_DAMAGE', 'Расслоения и механические повреждения на торцах'),
            ('CUT_SURFACE_DELAMINATION', 'Расслоения на поверхности реза'),
            ('OL_WINDING_TENSION_LOSS', 'Ослабление натяжения витков МП типа ОЛ'),
            ('WINDING_SHIFT', 'Смещение витков'),
            ('HIGH_ROUGHNESS', 'Повышенная шероховатость'),
        ]
        act_statuses = [
            ('CREATED_OTK', 'Создан ОТК', 10, False),
            ('KO_REVIEW', 'На рассмотрении КО', 20, False),
            ('TO_ANALYSIS', 'На анализе ТО', 30, False),
            ('OTK_REVIEW', 'Проверка ОТК', 40, False),
            ('ACTIONS_ASSIGNED', 'Мероприятия назначены', 50, False),
            ('CLOSED', 'Закрыт', 60, True),
            ('CANCELLED', 'Отменён', 70, True),
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
                'Reference data ready: 13 operations, 20 defect types, '
                '7 act statuses, 6 task statuses, 4 priorities.'
            )
        )
