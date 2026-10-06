'''`BoardCard.stage` is gone: a card stands in `column` of its `sub_board`.

Backwards, the column comes back with its default and is then filled from the
column's place among its sub-board's working columns — first → `TODO`,
second → `IN_PROGRESS`, any other and none → `REVIEW` — so rolling back this
one step returns every card to the stage it stands in. The `RunPython` is the
first operation so that, reversed, it runs last, once the field exists again.
'''

from django.conf import settings
from django.db import migrations, models


def restore_stage(apps, schema_editor):
    '''`stage` from the column's place among its sub-board's working columns.

    The same rule as `0006`'s reverse; copied, because a migration must not
    import another.
    '''
    BoardColumn = apps.get_model('boards', 'BoardColumn')
    BoardCard = apps.get_model('boards', 'BoardCard')

    stage_of = {}
    rank = {}
    working = BoardColumn.objects.filter(is_done=False).order_by('sub_board_id', 'position', 'pk')
    for column in working:
        index = rank.get(column.sub_board_id, 0)
        rank[column.sub_board_id] = index + 1
        stage_of[column.pk] = ('TODO', 'IN_PROGRESS')[index] if index < 2 else 'REVIEW'
    for stage in ('TODO', 'IN_PROGRESS'):
        ids = [pk for pk, value in stage_of.items() if value == stage]
        BoardCard.objects.filter(column_id__in=ids).update(stage=stage)
    BoardCard.objects.exclude(
        column_id__in=[pk for pk, value in stage_of.items() if value != 'REVIEW'],
    ).update(stage='REVIEW')


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0007_sub_board_required'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, restore_stage),
        migrations.AlterModelOptions(
            name='boardcard',
            options={
                'ordering': ['sub_board_id', 'column_id', 'position', 'pk'],
                'verbose_name': 'Карточка доски',
                'verbose_name_plural': 'Карточки досок',
            },
        ),
        migrations.RemoveIndex(
            model_name='boardcard',
            name='board_card_column_order',
        ),
        migrations.AddIndex(
            model_name='boardcard',
            index=models.Index(
                fields=['sub_board', 'column', 'position'], name='board_card_place'
            ),
        ),
        migrations.RemoveField(
            model_name='boardcard',
            name='stage',
        ),
    ]
