"""One protocol decision may now own more than one real task.

Nothing here touches a row's data. `protocol_action` loses its one-to-one
uniqueness and becomes an ordinary foreign key — on PostgreSQL that drops an
index, on SQLite it rebuilds the table copying every column, primary keys
included — so existing tasks keep their ids, their assignees and the decision
they came from, and no production task is recreated or renumbered.

What replaces the dropped uniqueness is stated explicitly instead: at most one
shared task per decision (`individual_assignee IS NULL`), and at most one task
per `(decision, individual assignee)` pair. Every task stored before this
migration has a NULL `individual_assignee`, so both constraints validate
against the existing data exactly as the old one-to-one did — a repeated
finalization still cannot produce a duplicate.

The source-shape check constraint is dropped and re-added rather than edited:
it is one constraint listing every valid shape, and the new column has to
appear in the act and approval branches as forbidden.
"""


import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_pdo_department'),
        ('acts', '0024_remove_act_defect_summary'),
        ('protocols', '0004_protocolaction_split_for_assignees'),
        ('references', '0003_simplify_task_statuses'),
        ('tasks', '0008_task_source_shape_constraint'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='task',
            name='task_source_relations_match_source_type',
        ),
        migrations.AddField(
            model_name='task',
            name='individual_assignee',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='individual_protocol_tasks', to=settings.AUTH_USER_MODEL, verbose_name='Персональный исполнитель'),
        ),
        migrations.AlterField(
            model_name='task',
            name='protocol_action',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='tasks', to='protocols.protocolaction', verbose_name='Задача протокола'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('act__isnull', False), ('individual_assignee__isnull', True), ('protocol__isnull', True), ('protocol_action__isnull', True), ('root_analysis__isnull', False), ('source_action__isnull', False), ('source_type', 'ACT')), models.Q(('act__isnull', True), ('individual_assignee__isnull', True), ('protocol__isnull', False), ('protocol_action__isnull', True), ('root_analysis__isnull', True), ('source_action__isnull', True), ('source_type', 'PROTOCOL_APPROVAL')), models.Q(('act__isnull', True), ('protocol__isnull', False), ('protocol_action__isnull', False), ('root_analysis__isnull', True), ('source_action__isnull', True), ('source_type', 'PROTOCOL_ACTION')), _connector='OR'), name='task_source_relations_match_source_type'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(condition=models.Q(('individual_assignee__isnull', True)), fields=('protocol_action',), name='unique_shared_protocol_action_task'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(fields=('protocol_action', 'individual_assignee'), name='unique_individual_protocol_action_task'),
        ),
    ]
