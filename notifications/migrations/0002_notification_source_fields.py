"""Step 1 of 4: add the source columns in a backward-compatible state.

Nothing is removed and nothing is relaxed here. `related_act` stays required,
so a running instance of the previous code keeps working against this schema,
and every existing row silently takes `source_type = 'ACT'` from the default —
which is what it already was in every sense but the column.
"""

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('protocols', '0003_protocolapproval'),
        ('tasks', '0008_task_source_shape_constraint'),
        ('notifications', '0001_initial'),
    ]

    operations = [
        # Choices only — the new protocol event codes. No stored value
        # changes and no data is touched.
        migrations.AlterField(
            model_name='notification',
            name='event_type',
            field=models.CharField(choices=[('ACT_SENT_TO_KO', 'Акт передан в КО'), ('ACT_SENT_TO_TO', 'Акт передан в ТО'), ('ACT_SENT_TO_OTK', 'Акт передан на проверку ОТК'), ('ACT_RETURNED_TO_OTK', 'Акт возвращён в ОТК'), ('ACT_RETURNED_TO_KO', 'Акт возвращён в КО'), ('ACT_RETURNED_TO_TO', 'Акт возвращён в ТО'), ('ACTION_ASSIGNED', 'Назначено мероприятие'), ('ACT_APPROVED', 'Акт утверждён'), ('COMMENT_ADDED', 'Добавлен комментарий'), ('PROTOCOL_APPROVAL_REQUIRED', 'Требуется согласование протокола'), ('PROTOCOL_RETURNED_FOR_REVISION', 'Протокол возвращён на доработку'), ('PROTOCOL_APPROVED', 'Протокол согласован'), ('PROTOCOL_TASK_ASSIGNED', 'Назначена задача по протоколу')], max_length=40, verbose_name='Тип события'),
        ),
        migrations.AddField(
            model_name='notification',
            name='source_type',
            field=models.CharField(
                choices=[('ACT', 'Акт'), ('PROTOCOL', 'Протокол'), ('TASK', 'Задача')],
                default='ACT',
                max_length=16,
                verbose_name='Тип источника',
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='related_protocol',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notifications',
                to='protocols.protocol',
                verbose_name='Связанный протокол',
            ),
        ),
        migrations.AddField(
            model_name='notification',
            name='related_task',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notifications',
                to='tasks.task',
                verbose_name='Связанная задача',
            ),
        ),
    ]
