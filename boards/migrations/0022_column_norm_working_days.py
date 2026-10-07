"""«Застой» becomes «Норматив этапа»: `BoardColumn.stale_after_days` is
renamed `norm_working_days`, every value kept as it was — the number stays,
its unit is now working days. The check (1–365 or NULL, never the closing
column) is the same, under a name that says what it guards."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0021_links_column_subscriptions_sums'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='boardcolumn',
            name='board_column_stale_days_valid',
        ),
        migrations.RenameField(
            model_name='boardcolumn',
            old_name='stale_after_days',
            new_name='norm_working_days',
        ),
        migrations.AlterField(
            model_name='boardcolumn',
            name='norm_working_days',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name='Норматив этапа, рабочих дней',
            ),
        ),
        migrations.AddConstraint(
            model_name='boardcolumn',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ('norm_working_days__isnull', True),
                    models.Q(
                        ('is_done', False),
                        ('norm_working_days__gte', 1),
                        ('norm_working_days__lte', 365),
                    ),
                    _connector='OR',
                ),
                name='board_column_norm_days_valid',
            ),
        ),
    ]
