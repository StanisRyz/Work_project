"""Board codes and card numbers, step three: required and unique.

`unique_board_code` — codes are stored upper case, so this is uniqueness
whatever the case; `unique_board_card_number` — one number per card of a
board.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('boards', '0012_backfill_codes_and_numbers'),
    ]

    operations = [
        migrations.AlterField(
            model_name='board',
            name='code',
            field=models.CharField(max_length=6, verbose_name='Код'),
        ),
        migrations.AlterField(
            model_name='boardcard',
            name='number',
            field=models.PositiveIntegerField(verbose_name='Номер'),
        ),
        migrations.AddConstraint(
            model_name='board',
            constraint=models.UniqueConstraint(fields=('code',), name='unique_board_code'),
        ),
        migrations.AddConstraint(
            model_name='boardcard',
            constraint=models.UniqueConstraint(fields=('board', 'number'), name='unique_board_card_number'),
        ),
    ]
