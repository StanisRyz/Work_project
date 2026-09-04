"""«Архив» → «Архивировано» on `SmkSource.Status`.

A label only: the stored values are still `ACTIVE`/`ARCHIVED`, no row is
touched and no column changes. It is here because Django tracks `choices` in
migration state, and because the record page, the registry pill and Django
Admin must all say the same word.

No data migration accompanies it. «Создана» and «Завершена» were never stored —
they were derived per read by `smk.selectors.describe_smk_state()` from task
counts, and dropping that derivation is what makes every live record read «В
работе» straight away.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smk', '0008_corrective_action_split_for_assignees'),
    ]

    operations = [
        migrations.AlterField(
            model_name='smksource',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'В работе'), ('ARCHIVED', 'Архивировано')], default='ACTIVE', max_length=16, verbose_name='Статус'),
        ),
    ]
