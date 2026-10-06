"""Every card stands on a sub-board: `BoardCard.sub_board` becomes NOT NULL.

`0006` gave every card one; nothing writes a card without it since.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0006_default_sub_boards'),
    ]

    operations = [
        migrations.AlterField(
            model_name='boardcard',
            name='sub_board',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='cards',
                to='boards.subboard',
                verbose_name='Поддоска',
            ),
        ),
    ]
