"""One act corrective action may now own more than one real task.

Nothing here touches a row's data. `source_action` loses its one-to-one
uniqueness and becomes an ordinary foreign key — on PostgreSQL that drops an
index, on SQLite it rebuilds the table copying every column, primary keys
included — so existing tasks keep their ids, their status, their completion
data, their assignees and the corrective action they came from. No production
task is recreated or renumbered, and protocol tasks are untouched throughout.

What replaces the dropped uniqueness is stated explicitly instead: at most one
shared task per corrective action (`individual_assignee IS NULL`), and at most
one per `(corrective action, individual assignee)`. Every act task stored
before this migration has a NULL `individual_assignee`, so both constraints
validate against the existing data exactly as the old one-to-one did.

The source-shape check constraint is dropped and re-added rather than edited:
it is one constraint listing every valid shape, and the `ACT` branch has to
stop forbidding `individual_assignee` now that an act task can be split.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_pdo_department'),
        ('acts', '0025_act_task_split'),
        ('protocols', '0005_protocol_collaboration'),
        ('references', '0003_simplify_task_statuses'),
        ('tasks', '0009_protocol_action_task_fanout'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='task',
            name='task_source_relations_match_source_type',
        ),
        migrations.AlterField(
            model_name='task',
            name='source_action',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='tasks', to='acts.actcorrectiveaction', verbose_name='Исходное корректирующее мероприятие'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('act__isnull', False), ('protocol__isnull', True), ('protocol_action__isnull', True), ('root_analysis__isnull', False), ('source_action__isnull', False), ('source_type', 'ACT')), models.Q(('act__isnull', True), ('individual_assignee__isnull', True), ('protocol__isnull', False), ('protocol_action__isnull', True), ('root_analysis__isnull', True), ('source_action__isnull', True), ('source_type', 'PROTOCOL_APPROVAL')), models.Q(('act__isnull', True), ('protocol__isnull', False), ('protocol_action__isnull', False), ('root_analysis__isnull', True), ('source_action__isnull', True), ('source_type', 'PROTOCOL_ACTION')), _connector='OR'), name='task_source_relations_match_source_type'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(condition=models.Q(('individual_assignee__isnull', True)), fields=('source_action',), name='unique_shared_act_action_task'),
        ),
        migrations.AddConstraint(
            model_name='task',
            constraint=models.UniqueConstraint(fields=('source_action', 'individual_assignee'), name='unique_individual_act_action_task'),
        ),
    ]
