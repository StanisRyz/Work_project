"""Every existing board gets the sub-board «Основная» with its four columns.

The four fixed columns a board had become four ordinary `BoardColumn` rows of
one `SubBoard`: «Сделать», «В работе», «На проверке» and «Готово», the last
one closing (`is_done`). Each card goes to «Основная» and to the column its
`stage` named; its `position` is kept, so the order inside every column is
exactly what it was. A completed or cancelled card is mapped the same way —
its `stage` was the column it returns to when reopened, and so is `column`.

Backwards, `stage` is restored from the column's place among the working
columns of its sub-board — first → `TODO`, second → `IN_PROGRESS`, any other
and none → `REVIEW` — and the sub-boards and columns this created are removed,
so the migration can run again. Historical models only, no events.
"""

from django.db import migrations


DEFAULT_SUB_BOARD = 'Основная'

# (stage, column name) in the order a board drew them; «Готово» was derived.
STAGE_COLUMNS = (
    ('TODO', 'Сделать'),
    ('IN_PROGRESS', 'В работе'),
    ('REVIEW', 'На проверке'),
)
DONE_COLUMN = 'Готово'


def create_default_sub_boards(apps, schema_editor):
    Board = apps.get_model('boards', 'Board')
    SubBoard = apps.get_model('boards', 'SubBoard')
    BoardColumn = apps.get_model('boards', 'BoardColumn')
    BoardCard = apps.get_model('boards', 'BoardCard')

    for board in Board.objects.order_by('pk').iterator():
        if SubBoard.objects.filter(board=board).exists():
            continue
        sub_board = SubBoard.objects.create(
            board=board, name=DEFAULT_SUB_BOARD, position=1, created_by_id=board.owner_id,
        )
        for position, (stage, name) in enumerate(STAGE_COLUMNS, start=1):
            column = BoardColumn.objects.create(
                sub_board=sub_board, name=name, position=position, is_done=False,
            )
            BoardCard.objects.filter(board=board, stage=stage).update(
                sub_board=sub_board, column=column,
            )
        BoardColumn.objects.create(
            sub_board=sub_board, name=DONE_COLUMN, position=len(STAGE_COLUMNS) + 1, is_done=True,
        )
        # A stage value outside the three (never written, but the column was
        # free text in the database) lands in the first working column.
        BoardCard.objects.filter(board=board, sub_board__isnull=True).update(
            sub_board=sub_board,
            column=BoardColumn.objects.get(sub_board=sub_board, position=1),
        )


def restore_stage(apps, schema_editor):
    """`stage` from the column's place among its sub-board's working columns."""
    BoardColumn = apps.get_model('boards', 'BoardColumn')
    BoardCard = apps.get_model('boards', 'BoardCard')

    stage_of = {}
    working = BoardColumn.objects.filter(is_done=False).order_by('sub_board_id', 'position', 'pk')
    rank = {}
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


def remove_default_sub_boards(apps, schema_editor):
    SubBoard = apps.get_model('boards', 'SubBoard')
    BoardColumn = apps.get_model('boards', 'BoardColumn')
    BoardCard = apps.get_model('boards', 'BoardCard')

    restore_stage(apps, schema_editor)
    BoardCard.objects.update(sub_board=None, column=None)
    BoardColumn.objects.all().delete()
    SubBoard.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0005_sub_boards_and_columns'),
    ]

    operations = [
        migrations.RunPython(create_default_sub_boards, remove_default_sub_boards),
    ]
