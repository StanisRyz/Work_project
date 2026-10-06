"""«Застой» of a working column: `BoardColumn.stale_after_days`.

A nullable column — NULL is «off» for every existing column, so nothing is
classified or backfilled — and a check constraint: a threshold is 1 to 365
days and only ever a working column's. Rolling back drops both.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0015_board_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='boardcolumn',
            name='stale_after_days',
            field=models.PositiveSmallIntegerField(
                blank=True, null=True, verbose_name='Застой: подсвечивать через, дней',
            ),
        ),
        migrations.AddConstraint(
            model_name='boardcolumn',
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ('stale_after_days__isnull', True),
                    models.Q(
                        ('is_done', False),
                        ('stale_after_days__gte', 1),
                        ('stale_after_days__lte', 365),
                    ),
                    _connector='OR',
                ),
                name='board_column_stale_days_valid',
            ),
        ),
    ]
