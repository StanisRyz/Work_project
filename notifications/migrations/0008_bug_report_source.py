"""The `BUG` notification source: a report from «Сообщить об ошибке».

A fourth shape beside `ACT`, `PROTOCOL` and `TASK` — the first that is not a
quality document. The check constraint is dropped and rebuilt because a new
branch is a new expression, not an alteration of the old one; every existing
row keeps its own shape and none is rewritten. `related_bug_report` is
nullable, so the column is added to a populated table without a default.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('acts', '0026_actcorrectiveaction_requires_attachment'),
        ('bugs', '0001_initial'),
        ('notifications', '0007_smk_task_event_type'),
        ('protocols', '0007_web_system_protocol_type'),
        ('tasks', '0017_smk_task_split'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='notification',
            name='notification_source_relations_match_source_type',
        ),
        migrations.AddField(
            model_name='notification',
            name='related_bug_report',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='bugs.bugreport', verbose_name='Связанное сообщение об ошибке'),
        ),
        migrations.AlterField(
            model_name='notification',
            name='event_type',
            field=models.CharField(choices=[('ACT_SENT_TO_KO', 'Акт передан в КО'), ('ACT_SENT_TO_TO', 'Акт передан в ТО'), ('ACT_SENT_TO_OTK', 'Акт передан на проверку ОТК'), ('ACT_RETURNED_TO_OTK', 'Акт возвращён в ОТК'), ('ACT_RETURNED_TO_KO', 'Акт возвращён в КО'), ('ACT_RETURNED_TO_TO', 'Акт возвращён в ТО'), ('ACTION_ASSIGNED', 'Назначено мероприятие'), ('ACT_APPROVED', 'Акт утверждён'), ('COMMENT_ADDED', 'Добавлен комментарий'), ('PROTOCOL_APPROVAL_REQUIRED', 'Требуется согласование протокола'), ('PROTOCOL_RETURNED_FOR_REVISION', 'Протокол возвращён на доработку'), ('PROTOCOL_APPROVED', 'Протокол согласован'), ('PROTOCOL_TASK_ASSIGNED', 'Назначена задача по протоколу'), ('ACT_REJECTION_ASSIGNED', 'Назначена задача ПДО по браку'), ('SMK_TASK_ASSIGNED', 'Назначена задача СМК'), ('BUG_REPORTED', 'Сообщение об ошибке в системе')], max_length=40, verbose_name='Тип события'),
        ),
        migrations.AlterField(
            model_name='notification',
            name='source_type',
            field=models.CharField(choices=[('ACT', 'Акт'), ('PROTOCOL', 'Протокол'), ('TASK', 'Задача'), ('BUG', 'Сообщение об ошибке')], default='ACT', max_length=16, verbose_name='Тип источника'),
        ),
        migrations.AddConstraint(
            model_name='notification',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('related_act__isnull', False), ('related_bug_report__isnull', True), ('related_protocol__isnull', True), ('related_task__isnull', True), ('source_type', 'ACT')), models.Q(('related_act__isnull', True), ('related_bug_report__isnull', True), ('related_protocol__isnull', False), ('related_task__isnull', True), ('source_type', 'PROTOCOL')), models.Q(('related_act__isnull', True), ('related_bug_report__isnull', True), ('related_protocol__isnull', True), ('related_task__isnull', False), ('source_type', 'TASK')), models.Q(('related_act__isnull', True), ('related_bug_report__isnull', False), ('related_protocol__isnull', True), ('related_task__isnull', True), ('source_type', 'BUG')), _connector='OR'), name='notification_source_relations_match_source_type'),
        ),
    ]
