"""«Разбить задачу по исполнителям» on an СМК мероприятие.

The same field `ProtocolAction` and `ActCorrectiveAction` already carry, so
СМК reuses the common `tasks.Task` split model instead of gaining one of its
own. `default=False` leaves every measure stored before it existed exactly as
it behaved: one shared task carrying every исполнитель.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smk', '0007_smk_content_superseded_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='smkcorrectiveaction',
            name='split_for_assignees',
            field=models.BooleanField(
                default=False, verbose_name='Разбить задачу по исполнителям',
            ),
        ),
    ]
