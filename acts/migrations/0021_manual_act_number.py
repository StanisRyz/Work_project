from django.db import migrations, models


class Migration(migrations.Migration):
    """`Act.number` becomes a user-entered business identifier.

    The unique constraint is dropped (two acts may legitimately share a public
    number, `Act.pk` remains the only unique key) and the per-year counter that
    backed the removed automatic numbering is deleted. Existing act numbers are
    never rewritten.
    """

    dependencies = [
        ('acts', '0020_actnumbersequence'),
    ]

    operations = [
        migrations.AlterField(
            model_name='act',
            name='number',
            field=models.CharField(
                blank=True, db_index=True, max_length=32, verbose_name='Номер акта'
            ),
        ),
        migrations.DeleteModel(name='ActNumberSequence'),
    ]
