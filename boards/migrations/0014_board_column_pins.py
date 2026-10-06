"""«Закреплённые исполнители» of a working column.

A new table (`BoardColumnPin`: column, user, unique together) behind
`BoardColumn.pinned_assignees`, and `BoardColumn.pinned_mode` — `ADD` for
every existing column, which with no pins changes nothing.
"""

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0013_board_code_card_number_required'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='boardcolumn',
            name='pinned_mode',
            field=models.CharField(
                choices=[('ADD', 'Добавить к исполнителям'), ('REPLACE', 'Заменить исполнителей')],
                default='ADD', max_length=8, verbose_name='Режим закрепления',
            ),
        ),
        migrations.CreateModel(
            name='BoardColumnPin',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('column', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='pins', to='boards.boardcolumn', verbose_name='Колонка')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='board_column_pins', to=settings.AUTH_USER_MODEL, verbose_name='Сотрудник')),
            ],
            options={
                'verbose_name': 'Закреплённый исполнитель колонки',
                'verbose_name_plural': 'Закреплённые исполнители колонок',
                'ordering': ['column', 'pk'],
                'constraints': [models.UniqueConstraint(fields=('column', 'user'), name='unique_board_column_pin')],
            },
        ),
        migrations.AddField(
            model_name='boardcolumn',
            name='pinned_assignees',
            field=models.ManyToManyField(blank=True, related_name='pinned_board_columns', through='boards.BoardColumnPin', to=settings.AUTH_USER_MODEL, verbose_name='Закреплённые исполнители'),
        ),
    ]
