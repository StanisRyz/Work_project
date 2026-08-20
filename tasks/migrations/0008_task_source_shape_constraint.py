"""Stage 4: put the source shapes beyond reach of any writer.

Added last on purpose. By this point every existing row is classified `ACT`
and still carries its three act relations, so the constraint validates against
real data instead of racing the backfill. Adding it fails loudly rather than
silently if any row is mixed — which is the intended behaviour for a
production table.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0007_relax_act_source_relations'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='task',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ('act__isnull', False),
                        ('protocol__isnull', True),
                        ('protocol_action__isnull', True),
                        ('root_analysis__isnull', False),
                        ('source_action__isnull', False),
                        ('source_type', 'ACT'),
                    )
                    | models.Q(
                        ('act__isnull', True),
                        ('protocol__isnull', False),
                        ('protocol_action__isnull', True),
                        ('root_analysis__isnull', True),
                        ('source_action__isnull', True),
                        ('source_type', 'PROTOCOL_APPROVAL'),
                    )
                    | models.Q(
                        ('act__isnull', True),
                        ('protocol__isnull', False),
                        ('protocol_action__isnull', False),
                        ('root_analysis__isnull', True),
                        ('source_action__isnull', True),
                        ('source_type', 'PROTOCOL_ACTION'),
                    )
                ),
                name='task_source_relations_match_source_type',
            ),
        ),
    ]
