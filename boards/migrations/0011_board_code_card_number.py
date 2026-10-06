"""Board codes and card numbers, step one of three: the columns, nullable.

`Board.code` («ZAP») and `BoardCard.number` («12» in «ZAP-12») are added
empty so the existing rows can be classified by `0012` before `0013` makes
them required and unique — the order `AGENTS.md` asks for a live table.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0010_backfill_card_events'),
    ]

    operations = [
        migrations.AddField(
            model_name='board',
            name='code',
            field=models.CharField(max_length=6, null=True, verbose_name='Код'),
        ),
        migrations.AddField(
            model_name='boardcard',
            name='number',
            field=models.PositiveIntegerField(null=True, verbose_name='Номер'),
        ),
    ]
