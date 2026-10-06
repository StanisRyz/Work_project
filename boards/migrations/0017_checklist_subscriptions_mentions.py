"""A card's «Чек-лист», its subscribers and the people a message mentions.

Three new tables — `BoardCardChecklistItem`, `BoardCardSubscription`,
`BoardCardCommentMention` — and the journal's new kind `CHECKLIST` (a choice,
no schema change). Nothing exists to classify or backfill: every card starts
with an empty list, no subscriber and no mention. Rolling back drops the three
tables; the `CHECKLIST` journal entries written meanwhile stay as rows whose
kind the older code shows by its code.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0016_column_stale_days"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
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
                ],
                max_length=20,
                verbose_name="Событие",
            ),
        ),
        migrations.CreateModel(
            name="BoardCardChecklistItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("text", models.CharField(max_length=200, verbose_name="Пункт")),
                ("position", models.PositiveIntegerField(verbose_name="Позиция")),
                ("is_done", models.BooleanField(default=False, verbose_name="Сделано")),
                (
                    "done_at",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Отмечено"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Добавлен"),
                ),
                (
                    "card",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="checklist",
                        to="boards.boardcard",
                        verbose_name="Карточка",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Добавил",
                    ),
                ),
                (
                    "done_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Отметил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Пункт чек-листа",
                "verbose_name_plural": "Пункты чек-листов",
                "ordering": ["card_id", "position", "pk"],
                "indexes": [
                    models.Index(
                        fields=["card", "position"], name="board_checklist_place"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="BoardCardCommentMention",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "comment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="mentions",
                        to="boards.boardcardcomment",
                        verbose_name="Сообщение",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="board_card_mentions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Упомянут",
                    ),
                ),
            ],
            options={
                "verbose_name": "Упоминание в сообщении",
                "verbose_name_plural": "Упоминания в сообщениях",
                "ordering": ["comment_id", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("comment", "user"), name="unique_board_comment_mention"
                    )
                ],
            },
        ),
        migrations.CreateModel(
            name="BoardCardSubscription",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Подписан"),
                ),
                (
                    "card",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="subscriptions",
                        to="boards.boardcard",
                        verbose_name="Карточка",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="board_card_subscriptions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Подписчик",
                    ),
                ),
            ],
            options={
                "verbose_name": "Подписка на карточку",
                "verbose_name_plural": "Подписки на карточки",
                "ordering": ["card_id", "created_at", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("card", "user"), name="unique_board_card_subscription"
                    )
                ],
            },
        ),
    ]
