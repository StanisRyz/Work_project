"""Сроки и отклонения: `BoardCard.original_due_date` and `BoardCardDueChange`.

One nullable column — left NULL on every existing card: its current срок is
not necessarily its first, and none is made up — and one new table, the
history of every move of a card's срок with its reason
(`references.DeviationReason`, `PROTECT`). Nothing to backfill. The reverse
drops both; the history written meanwhile goes with the table.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0019_card_subtasks"),
        ("references", "0005_deviation_reasons"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="boardcard",
            name="original_due_date",
            field=models.DateField(blank=True, null=True, verbose_name="Исходный срок"),
        ),
        migrations.CreateModel(
            name="BoardCardDueChange",
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
                    "old_due",
                    models.DateField(blank=True, null=True, verbose_name="Было"),
                ),
                (
                    "new_due",
                    models.DateField(blank=True, null=True, verbose_name="Стало"),
                ),
                (
                    "comment",
                    models.CharField(
                        blank=True, max_length=500, verbose_name="Комментарий"
                    ),
                ),
                (
                    "changed_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Когда"),
                ),
                (
                    "card",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="due_changes",
                        to="boards.boardcard",
                        verbose_name="Карточка",
                    ),
                ),
                (
                    "changed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="board_due_changes",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто перенёс",
                    ),
                ),
                (
                    "reason",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="board_due_changes",
                        to="references.deviationreason",
                        verbose_name="Причина",
                    ),
                ),
            ],
            options={
                "verbose_name": "Перенос срока карточки",
                "verbose_name_plural": "Переносы сроков карточек",
                "ordering": ["changed_at", "pk"],
                "indexes": [
                    models.Index(
                        fields=["card", "changed_at"], name="board_due_change_time"
                    )
                ],
            },
        ),
    ]
