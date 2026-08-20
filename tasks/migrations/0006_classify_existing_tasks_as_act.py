"""Stage 2: state, as data, that every task already in the table is an act task.

The column default in stage 1 already writes `ACT`, but the classification of
existing production rows is a business decision and is recorded as one here:
this is the step that can be read, reviewed and reasoned about, and the step
that would need changing if a future table ever held anything else.

It only writes `source_type`. Statuses, assignees, deadlines, completion data
and primary keys are untouched, and no row is created from `ProtocolAction` —
protocol tasks are a later stage and none exist.
"""

from django.db import migrations


def classify_existing_tasks_as_act(apps, schema_editor):
    Task = apps.get_model('tasks', 'Task')
    # Every task that exists at this point came from an approved act, by
    # construction: act creation was the only writer of this table.
    Task.objects.exclude(source_type='ACT').update(source_type='ACT')


class Migration(migrations.Migration):

    dependencies = [
        ('tasks', '0005_task_source_type_and_protocol_relations'),
    ]

    operations = [
        # Irreversible only in the sense that there is nothing to undo: the
        # column itself is removed by reversing stage 1.
        migrations.RunPython(classify_existing_tasks_as_act, migrations.RunPython.noop),
    ]
