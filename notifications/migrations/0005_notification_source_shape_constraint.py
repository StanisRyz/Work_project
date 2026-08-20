"""Step 4 of 4: the source-shape check constraint.

Added last, once `related_act` is nullable and every existing row is a valid
`ACT` shape. From here the database itself refuses a mixed or sourceless
notification, whatever writes it.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0004_relax_notification_act_relation'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='notification',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        ('related_act__isnull', False),
                        ('related_protocol__isnull', True),
                        ('related_task__isnull', True),
                        ('source_type', 'ACT'),
                    )
                    | models.Q(
                        ('related_act__isnull', True),
                        ('related_protocol__isnull', False),
                        ('related_task__isnull', True),
                        ('source_type', 'PROTOCOL'),
                    )
                    | models.Q(
                        ('related_act__isnull', True),
                        ('related_protocol__isnull', True),
                        ('related_task__isnull', False),
                        ('source_type', 'TASK'),
                    )
                ),
                name='notification_source_relations_match_source_type',
            ),
        ),
    ]
