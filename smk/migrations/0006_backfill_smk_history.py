"""Give records stored before the trail existed the events they really have.

Only facts already in the database are written: `SmkSource.created_at` and
`created_by` for «создана», and each `Task`'s own `created_at`/`created_by`
for the задача it is. Nothing is invented — a record archived before this
migration cannot exist, because `status` and the archive action arrived in
`0004` together with no way to use them yet, so no `ARCHIVED` event is
backfilled.

`created_at` is `auto_now_add`, so each row is inserted and then stamped: that
is the only way to give an event the time of the thing it describes rather
than the time of the migration.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    SmkSource = apps.get_model('smk', 'SmkSource')
    SmkHistoryEvent = apps.get_model('smk', 'SmkHistoryEvent')
    Task = apps.get_model('tasks', 'Task')

    for source in SmkSource.objects.all():
        # Idempotent: a re-run on a database that already has the trail adds
        # nothing.
        if SmkHistoryEvent.objects.filter(source=source).exists():
            continue
        label = f'СМК №{source.pk}'
        stamped = []
        for index, task in enumerate(
            Task.objects.filter(smk_source=source).order_by('smk_action__display_order', 'pk')
        ):
            stamped.append((
                SmkHistoryEvent.objects.create(
                    source=source,
                    actor_id=task.created_by_id,
                    event_type='TASK_CREATED',
                    message=f'По мероприятию №{index + 1} создана задача №{task.pk}.',
                ),
                task.created_at,
            ))
        stamped.append((
            SmkHistoryEvent.objects.create(
                source=source,
                actor_id=source.created_by_id,
                event_type='CREATED',
                message=(
                    f'{label} создана: несоответствий — '
                    f'{source.non_conformities.count()}, корректирующих мероприятий — '
                    f'{source.actions.count()}.'
                ),
            ),
            source.created_at,
        ))
        for event, created_at in stamped:
            SmkHistoryEvent.objects.filter(pk=event.pk).update(created_at=created_at)


class Migration(migrations.Migration):

    dependencies = [
        ('smk', '0005_smk_history_event'),
        ('tasks', '0001_initial'),
    ]

    # No reverse: the events are removed with the table by `0005`, and deleting
    # a trail to «undo» a backfill would destroy events written since.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
