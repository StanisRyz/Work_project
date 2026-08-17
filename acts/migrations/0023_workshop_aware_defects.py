from django.db import migrations, models
import django.db.models.deletion


def normalize_pir_defects(apps, schema_editor):
    """Drop МП-only data from ПиР defects before the constraint is added.

    Rows moved to ПиР by `0022_pir_workshop` kept the operation, party number
    and description of the former «Цех трансформаторов», which the ПиР workshop
    does not collect.
    """
    apps.get_model('acts', 'ActDefect').objects.filter(workshop='PIR_SHOP').exclude(
        operation__isnull=True,
        mp_type='',
        party_number='',
        description='',
    ).update(operation=None, mp_type='', party_number='', description='')


def check_defect_quantities(apps, schema_editor):
    """Fail with an actionable message instead of an opaque IntegrityError."""
    offending = list(
        apps.get_model('acts', 'ActDefect')
        .objects.filter(
            checked_quantity__isnull=False,
            nonconforming_quantity__isnull=False,
            nonconforming_quantity__gt=models.F('checked_quantity'),
        )
        .values_list('pk', flat=True)[:20]
    )
    if offending:
        raise RuntimeError(
            'ActDefect rows have nonconforming_quantity > checked_quantity and must '
            f'be corrected by hand before this migration can apply: {offending}.'
        )


class Migration(migrations.Migration):
    """Phase 1 of the workshop-aware defect architecture.

    `ActDefect` owns defect data, so `Act.defect_type` — a summary of the first
    defect — is no longer written and becomes optional. The legacy summary
    columns themselves stay untouched for rollback.
    """

    dependencies = [
        ('acts', '0022_pir_workshop'),
        ('references', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='act',
            name='defect_type',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to='references.defecttype',
                verbose_name='Вид дефекта',
            ),
        ),
        migrations.RunPython(normalize_pir_defects, migrations.RunPython.noop),
        migrations.RunPython(check_defect_quantities, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='actdefect',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(checked_quantity__isnull=True)
                    | models.Q(nonconforming_quantity__isnull=True)
                    | models.Q(nonconforming_quantity__lte=models.F('checked_quantity'))
                ),
                name='act_defect_nonconforming_within_checked',
            ),
        ),
        migrations.AddConstraint(
            model_name='actdefect',
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(workshop='PIR_SHOP')
                    | models.Q(
                        operation__isnull=True,
                        mp_type='',
                        party_number='',
                        description='',
                    )
                ),
                name='act_defect_pir_without_mp_only_data',
            ),
        ),
    ]
