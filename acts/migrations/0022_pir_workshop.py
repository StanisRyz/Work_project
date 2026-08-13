from django.db import migrations, models
import django.db.models.deletion


def transformers_to_pir(apps, schema_editor):
    """Move existing «Цех трансформаторов» defects to the ПиР workshop."""
    apps.get_model('acts', 'ActDefect').objects.filter(
        workshop='TRANSFORMERS_SHOP'
    ).update(workshop='PIR_SHOP')


def pir_to_transformers(apps, schema_editor):
    apps.get_model('acts', 'ActDefect').objects.filter(
        workshop='PIR_SHOP'
    ).update(workshop='TRANSFORMERS_SHOP')


class Migration(migrations.Migration):
    """ПиР replaces the transformers workshop and relaxes MP-only fields.

    ПиР defects do not carry an operation, a party number or a description, so
    those columns become optional instead of being filled with placeholders.
    Existing rows keep their values.
    """

    dependencies = [
        ('acts', '0021_manual_act_number'),
        ('references', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='actdefect',
            name='workshop',
            field=models.CharField(
                blank=True,
                choices=[('MP_SHOP', 'Цех МП'), ('PIR_SHOP', 'Цех ПиР')],
                max_length=32,
                verbose_name='Цех/поставщик',
            ),
        ),
        migrations.RunPython(transformers_to_pir, pir_to_transformers),
        migrations.AlterField(
            model_name='actdefect',
            name='description',
            field=models.TextField(blank=True, verbose_name='Описание дефекта'),
        ),
        migrations.AlterField(
            model_name='act',
            name='description',
            field=models.TextField(blank=True, verbose_name='Описание'),
        ),
        migrations.AlterField(
            model_name='act',
            name='party_number',
            field=models.CharField(blank=True, max_length=120, verbose_name='Номер партии'),
        ),
        migrations.AlterField(
            model_name='act',
            name='operation',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to='references.operation',
                verbose_name='Операция',
            ),
        ),
    ]
