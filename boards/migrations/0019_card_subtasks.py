"""«Подзадачи»: `BoardCard.parent`, and the parent's journal entry `SUBTASK`.

One nullable column and one check constraint — a subtask stands in no column
(`board_subtask_no_column`) — plus the new choice of `BoardCardEvent.kind`
(choices only, the column is unchanged). Nothing to classify or backfill:
every existing card is a card of the board (`parent` NULL), and the
constraint holds for all of them. «One level» is not a constraint: a check
cannot read another row, so `BoardCard.clean()` and the services say it.
Rolling back drops the constraint and the column: the subtasks stay cards
of their sub-board (with no column, so a reopened one would stand in the first
working column), their `SUBTASK` entries stay readable rows.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0018_chat_files"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="boardcard",
            name="parent",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="subtasks",
                to="boards.boardcard",
                verbose_name="Карточка",
            ),
        ),
        migrations.AlterField(
            model_name="boardcardevent",
            name="kind",
            field=models.CharField(
                choices=[
                    ("CREATED", "Создание"),
                    ("EDITED", "Изменение"),
                    ("MOVED", "Перенос"),
                    ("COMPLETED", "Завершение"),
                    ("REOPENED", "Возврат в работу"),
                    ("CANCELLED", "Отмена"),
                    ("CHECKLIST", "Чек-лист"),
                    ("SUBTASK", "Подзадача"),
                ],
                max_length=20,
                verbose_name="Событие",
            ),
        ),
        migrations.AddConstraint(
            model_name="boardcard",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("parent__isnull", True), ("column__isnull", True), _connector="OR"
                ),
                name="board_subtask_no_column",
            ),
        ),
    ]
