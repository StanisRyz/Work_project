"""Give cards stored before the journal existed the entries they really have.

Only facts already in the database are written: the card's own
`created_by`/`created_at` for «создание», its task's
`completed_by`/`completed_at` for «завершение» and
`cancelled_by`/`cancelled_at` for «отмена». Nothing is invented — no column a
card was created in (that was never stored), no move, no edit and no return to
work, since none of them left a trace; a «завершение» or «отмена» whose author
or time is missing is skipped rather than guessed.

`created_at` is `auto_now_add`, so each row is inserted and then stamped, as
`smk.0006` does: that is the only way to give an entry the time of the thing
it describes rather than the time of the migration. A re-run adds nothing — a
card that already has an entry of a kind gets no second one.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    BoardCard = apps.get_model('boards', 'BoardCard')
    BoardCardEvent = apps.get_model('boards', 'BoardCardEvent')
    Task = apps.get_model('tasks', 'Task')

    tasks = {
        task.board_card_id: task
        for task in Task.objects.filter(source_type='BOARD', board_card__isnull=False)
    }
    for card in BoardCard.objects.order_by('pk'):
        existing = set(BoardCardEvent.objects.filter(card=card).values_list('kind', flat=True))
        facts = [('CREATED', card.created_by_id, card.created_at)]
        task = tasks.get(card.pk)
        if task is not None:
            facts.append(('COMPLETED', task.completed_by_id, task.completed_at))
            facts.append(('CANCELLED', task.cancelled_by_id, task.cancelled_at))
        for kind, actor_id, when in facts:
            if kind in existing or actor_id is None or when is None:
                continue
            event = BoardCardEvent.objects.create(card=card, actor_id=actor_id, kind=kind, details={})
            BoardCardEvent.objects.filter(pk=event.pk).update(created_at=when)


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0009_board_card_event'),
        ('tasks', '0021_board_task_department_free'),
    ]

    # No reverse: the entries go with the table when `0009` is reversed, and
    # deleting a journal to «undo» a backfill would destroy entries written since.
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
