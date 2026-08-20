"""Stage 3: drop `NOT NULL` from the act source relations.

Only the nullability changes — the columns, their names, their targets and
their `PROTECT` behaviour stay exactly as they were, and `source_action` stays
one-to-one, so an `ActCorrectiveAction` still cannot produce a second task.

Nothing is relaxed in the domain by this step: after stage 4 an `ACT` task
still requires all three relations. They become nullable only so a protocol
task, which has none of them, can be represented at all.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('acts', '0015_act_approved_at_act_approved_by_and_more'),
        ('tasks', '0006_classify_existing_tasks_as_act'),
    ]

    operations = [
        migrations.AlterField(
            model_name='task',
            name='act',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='tasks',
                to='acts.act',
                verbose_name='Акт',
            ),
        ),
        migrations.AlterField(
            model_name='task',
            name='root_analysis',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='tasks',
                to='acts.actrootanalysis',
                verbose_name='Корневая причина',
            ),
        ),
        migrations.AlterField(
            model_name='task',
            name='source_action',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='task',
                to='acts.actcorrectiveaction',
                verbose_name='Исходное корректирующее мероприятие',
            ),
        ),
    ]
