"""Step 3 of 4: allow `related_act` to be NULL.

Only reachable once every stored row is classified as `ACT`, which 0003
guarantees. The field keeps its name and its `related_name` so existing act
code and queries are unaffected; nullability exists purely so protocol- and
task-sourced notifications can exist at all.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0003_classify_existing_notifications_as_act'),
    ]

    operations = [
        migrations.AlterField(
            model_name='notification',
            name='related_act',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notifications',
                to='acts.act',
                verbose_name='Связанный акт',
            ),
        ),
    ]
