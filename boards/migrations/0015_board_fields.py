"""A board's own card fields: `BoardField`, `BoardFieldOption`, `BoardCardFieldValue`.

Three new tables and their constraints — a known kind, a known colour, one
value per card and field, and exactly one value column filled. Nothing
existing is touched, so the reverse simply drops the tables.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("boards", "0014_board_column_pins"),
    ]

    operations = [
        migrations.CreateModel(
            name="BoardField",
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
                ("name", models.CharField(max_length=60, verbose_name="Название")),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("TEXT", "Текст"),
                            ("NUMBER", "Число"),
                            ("DATE", "Дата"),
                            ("SELECT", "Список"),
                        ],
                        max_length=8,
                        verbose_name="Вид",
                    ),
                ),
                ("position", models.PositiveIntegerField(verbose_name="Позиция")),
                (
                    "show_on_tile",
                    models.BooleanField(
                        default=True, verbose_name="Показывать на плитке"
                    ),
                ),
                (
                    "is_archived",
                    models.BooleanField(default=False, verbose_name="В архиве"),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создано"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Обновлено"),
                ),
                (
                    "board",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="fields",
                        to="boards.board",
                        verbose_name="Доска",
                    ),
                ),
            ],
            options={
                "verbose_name": "Поле карточек",
                "verbose_name_plural": "Поля карточек",
                "ordering": ["board_id", "position", "pk"],
            },
        ),
        migrations.CreateModel(
            name="BoardFieldOption",
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
                ("label", models.CharField(max_length=60, verbose_name="Подпись")),
                (
                    "color",
                    models.CharField(
                        choices=[
                            ("gray", "Серый"),
                            ("blue", "Синий"),
                            ("green", "Зелёный"),
                            ("yellow", "Жёлтый"),
                            ("orange", "Оранжевый"),
                            ("red", "Красный"),
                            ("purple", "Фиолетовый"),
                            ("teal", "Бирюзовый"),
                        ],
                        default="gray",
                        max_length=10,
                        verbose_name="Цвет",
                    ),
                ),
                ("position", models.PositiveIntegerField(verbose_name="Позиция")),
                (
                    "is_archived",
                    models.BooleanField(default=False, verbose_name="В архиве"),
                ),
                (
                    "field",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="options",
                        to="boards.boardfield",
                        verbose_name="Поле",
                    ),
                ),
            ],
            options={
                "verbose_name": "Вариант поля",
                "verbose_name_plural": "Варианты полей",
                "ordering": ["field_id", "position", "pk"],
            },
        ),
        migrations.CreateModel(
            name="BoardCardFieldValue",
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
                    "value_text",
                    models.CharField(
                        blank=True, max_length=500, null=True, verbose_name="Текст"
                    ),
                ),
                (
                    "value_number",
                    models.DecimalField(
                        blank=True,
                        decimal_places=4,
                        max_digits=18,
                        null=True,
                        verbose_name="Число",
                    ),
                ),
                (
                    "value_date",
                    models.DateField(blank=True, null=True, verbose_name="Дата"),
                ),
                (
                    "card",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="field_values",
                        to="boards.boardcard",
                        verbose_name="Карточка",
                    ),
                ),
                (
                    "field",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="values",
                        to="boards.boardfield",
                        verbose_name="Поле",
                    ),
                ),
                (
                    "option",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="values",
                        to="boards.boardfieldoption",
                        verbose_name="Вариант",
                    ),
                ),
            ],
            options={
                "verbose_name": "Значение поля карточки",
                "verbose_name_plural": "Значения полей карточек",
                "ordering": ["card_id", "field_id"],
            },
        ),
        migrations.AddConstraint(
            model_name="boardfield",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["TEXT", "NUMBER", "DATE", "SELECT"])),
                name="board_field_kind_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="boardfieldoption",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "color__in",
                        [
                            "gray",
                            "blue",
                            "green",
                            "yellow",
                            "orange",
                            "red",
                            "purple",
                            "teal",
                        ],
                    )
                ),
                name="board_field_option_color_known",
            ),
        ),
        migrations.AddConstraint(
            model_name="boardcardfieldvalue",
            constraint=models.UniqueConstraint(
                fields=("card", "field"), name="unique_board_card_field_value"
            ),
        ),
        migrations.AddConstraint(
            model_name="boardcardfieldvalue",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("option__isnull", True),
                        ("value_date__isnull", True),
                        ("value_number__isnull", True),
                        ("value_text__isnull", False),
                        models.Q(("value_text", ""), _negated=True),
                    ),
                    models.Q(
                        ("option__isnull", True),
                        ("value_date__isnull", True),
                        ("value_number__isnull", False),
                        ("value_text__isnull", True),
                    ),
                    models.Q(
                        ("option__isnull", True),
                        ("value_date__isnull", False),
                        ("value_number__isnull", True),
                        ("value_text__isnull", True),
                    ),
                    models.Q(
                        ("option__isnull", False),
                        ("value_date__isnull", True),
                        ("value_number__isnull", True),
                        ("value_text__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="board_card_field_value_exactly_one",
            ),
        ),
    ]
