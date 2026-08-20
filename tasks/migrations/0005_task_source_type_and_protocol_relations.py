"""Stage 1 of making `Task` source-aware: add, never rewrite.

Purely additive and backward-compatible. The existing act relations keep their
`NOT NULL` columns here, so code running against the old schema during the
deploy still writes valid rows; `source_type` arrives with the `ACT` default so
every row already in the table gets a correct value from the column default
itself, and the two protocol relations arrive nullable and unused. No existing
row is deleted, recreated or renumbered by this migration.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('protocols', '0002_quality_protocol_type'),
        ('tasks', '0004_task_updated_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='task',
            name='source_type',
            field=models.CharField(
                choices=[
                    ('ACT', 'По акту'),
                    ('PROTOCOL_APPROVAL', 'Согласование протокола'),
                    ('PROTOCOL_ACTION', 'По протоколу'),
                ],
                default='ACT',
                max_length=32,
                verbose_name='Тип источника',
            ),
        ),
        migrations.AddField(
            model_name='task',
            name='protocol',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='tasks',
                to='protocols.protocol',
                verbose_name='Протокол',
            ),
        ),
        migrations.AddField(
            model_name='task',
            name='protocol_action',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='task',
                to='protocols.protocolaction',
                verbose_name='Задача протокола',
            ),
        ),
    ]
