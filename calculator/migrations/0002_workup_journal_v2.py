"""«Проработка» V2: calculation signatures, row source, 1С and the employee.

Nothing is deleted or recreated here. Existing rows keep their primary keys
and every stored production number; they gain a signature derived from the
values they already carry and become `CALCULATOR` rows, so the first
recalculation of a core already in the journal still finds it instead of
adding a second line.

The one exception is a legacy pair of rows that turn out to describe the very
same calculation case under two different names — impossible to create from
the tab, but possible in imported data. The later row is kept as `IMPORT` so
it survives untouched while the earlier one owns the signature.
"""
from django.db import migrations, models

# A local copy of `calculator.models.build_calculation_signature()`, on
# purpose: a migration must keep computing what it computed the day it ran,
# whatever the application does later.
_DECIMALS = 6


def _signature(entry):
    calibration = 0.0
    if entry.calibration_enabled and entry.calibration_diameter_mm:
        calibration = max(float(entry.calibration_diameter_mm), 0.0)
    return '|'.join(
        f'{round(float(value), _DECIMALS):.{_DECIMALS}f}'
        for value in (
            entry.d, entry.outer_diameter, entry.b, entry.tape_thickness_mm, calibration,
        )
    )


def fill_signatures(apps, schema_editor):
    WindingEntry = apps.get_model('calculator', 'WindingEntry')
    claimed = set()
    for entry in WindingEntry.objects.order_by('pk').iterator():
        signature = _signature(entry)
        entry.calculation_signature = signature
        entry.source = 'IMPORT' if signature in claimed else 'CALCULATOR'
        claimed.add(signature)
        entry.save(update_fields=['calculation_signature', 'source'])


class Migration(migrations.Migration):

    dependencies = [
        ('calculator', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='windingentry',
            name='calculation_signature',
            field=models.CharField(db_index=True, default='', max_length=120, verbose_name='Подпись расчётного случая'),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='windingentry',
            name='source',
            field=models.CharField(
                choices=[('CALCULATOR', 'Калькулятор'), ('MANUAL', 'Добавлено вручную'), ('IMPORT', 'Импорт')],
                db_index=True, default='CALCULATOR', max_length=16, verbose_name='Источник',
            ),
        ),
        migrations.AddField(
            model_name='windingentry',
            name='one_c_expression',
            field=models.CharField(blank=True, default='', max_length=120, verbose_name='1С, выражение'),
        ),
        migrations.AddField(
            model_name='windingentry',
            name='one_c_hours',
            field=models.FloatField(blank=True, null=True, verbose_name='1С, ч'),
        ),
        migrations.AddField(
            model_name='windingentry',
            name='employee_name',
            field=models.CharField(blank=True, default='', max_length=120, verbose_name='Сотрудник'),
        ),
        # The name alone stops being an identity: one core may be worked out
        # with several tapes, and manual rows may repeat it freely.
        migrations.AlterField(
            model_name='windingentry',
            name='case_key',
            field=models.CharField(db_index=True, max_length=120, verbose_name='Ключ имени'),
        ),
        migrations.RunPython(fill_signatures, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='windingentry',
            constraint=models.UniqueConstraint(
                condition=models.Q(('source', 'CALCULATOR')),
                fields=('calculation_signature',),
                name='calculator_unique_calculator_case',
            ),
        ),
    ]
