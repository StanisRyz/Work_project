"""Files in a card's «Чат»: `BoardCardFile`, and a message that is files alone.

One new table — the board's own files, each on one message of one card,
stored under `boards/files/<card_id>/` — and `BoardCardComment.text` may be
empty now (a message of files only; the service still refuses one with
neither). Nothing to classify or backfill: the task attachments cards
already have stay `tasks.TaskAttachment` and are shown in the chat as they
are, never copied. Rolling back drops the table (the files under
`boards/files/` stay on the disk, unreferenced) and makes the text required
again — a message of files only, written meanwhile, keeps its empty text.
"""

import boards.models
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0017_checklist_subscriptions_mentions"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="boardcardcomment",
            name="text",
            field=models.TextField(blank=True, verbose_name="Текст"),
        ),
        migrations.CreateModel(
            name="BoardCardFile",
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
                    "file",
                    models.FileField(
                        blank=True,
                        max_length=255,
                        upload_to=boards.models.board_card_file_upload_to,
                        verbose_name="Файл",
                    ),
                ),
                (
                    "original_name",
                    models.CharField(max_length=255, verbose_name="Исходное имя файла"),
                ),
                ("size", models.PositiveIntegerField(default=0, verbose_name="Размер")),
                (
                    "content_type",
                    models.CharField(
                        blank=True, max_length=120, verbose_name="Тип содержимого"
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Загружен"),
                ),
                (
                    "deleted_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Удалён"),
                ),
                (
                    "card",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="files",
                        to="boards.boardcard",
                        verbose_name="Карточка",
                    ),
                ),
                (
                    "comment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="files",
                        to="boards.boardcardcomment",
                        verbose_name="Сообщение",
                    ),
                ),
                (
                    "deleted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Удалил",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="board_card_files",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Загрузил",
                    ),
                ),
            ],
            options={
                "verbose_name": "Файл в чате карточки",
                "verbose_name_plural": "Файлы в чатах карточек",
                "ordering": ["comment_id", "pk"],
                "indexes": [
                    models.Index(
                        fields=["card", "created_at"], name="board_card_file_time"
                    )
                ],
            },
        ),
    ]
