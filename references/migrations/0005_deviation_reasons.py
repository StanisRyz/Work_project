"""«Причины отклонений»: `DeviationReason`, and the eight reasons a plant starts
with.

The table and an idempotent seed keyed on `code` (`update_or_create`), so a
second run — or `seed_references` before or after it — changes nothing.
The list is written out here, not imported: a migration must keep doing what
it did when the reasons in the code change. Its reverse drops the table; the
seed's own reverse is a noop.
"""

from django.db import migrations, models


REASONS = (
    ('MATERIAL', 'Ждём материал (снабжение)', 10),
    ('DESIGN', 'Нет КД / ждём конструктора', 20),
    ('DEFECT', 'Брак', 30),
    ('EQUIPMENT', 'Оборудование', 40),
    ('PAYMENT', 'Ждём оплату', 50),
    ('CUSTOMER_CHANGE', 'Изменение заказа покупателем', 60),
    ('CAPACITY', 'Загрузка цеха', 70),
    ('OTHER', 'Другое', 80),
)


def seed_reasons(apps, schema_editor):
    DeviationReason = apps.get_model('references', 'DeviationReason')
    for code, name, order in REASONS:
        DeviationReason.objects.update_or_create(
            code=code, defaults={'name': name, 'display_order': order, 'is_active': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ("references", "0004_cancelled_task_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="DeviationReason",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "code",
                    models.CharField(max_length=64, unique=True, verbose_name="Код"),
                ),
                ("name", models.CharField(max_length=160, verbose_name="Название")),
                (
                    "is_active",
                    models.BooleanField(default=True, verbose_name="Активна"),
                ),
                (
                    "display_order",
                    models.PositiveIntegerField(default=100, verbose_name="Порядок"),
                ),
            ],
            options={
                "verbose_name": "Причина отклонения",
                "verbose_name_plural": "Причины отклонений",
                "ordering": ["display_order", "name"],
            },
        ),
        migrations.RunPython(seed_reasons, migrations.RunPython.noop),
    ]
