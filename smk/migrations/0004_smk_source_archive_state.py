"""Where an СМК record is filed.

`status` defaults to `ACTIVE`, so every record stored before this migration
lands in «Работа» — which is where it was, the registry simply did not exist
yet. Nothing is backfilled into `archived_at`/`archived_by`: no record had ever
been archived, and inventing an archivist would store a fabricated fact.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smk', '0003_audit_date_and_non_conformity_link'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='smksource',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Архивирован'),
        ),
        migrations.AddField(
            model_name='smksource',
            name='archived_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='archived_smk_sources', to=settings.AUTH_USER_MODEL, verbose_name='Архивировал'),
        ),
        migrations.AddField(
            model_name='smksource',
            name='status',
            field=models.CharField(choices=[('ACTIVE', 'В работе'), ('ARCHIVED', 'Архив')], default='ACTIVE', max_length=16, verbose_name='Статус'),
        ),
    ]
