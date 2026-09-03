# Created for the СМК record's own audit trail. Generated on 2026-09-03 06:26

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('smk', '0004_smk_source_archive_state'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='SmkHistoryEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('event_type', models.CharField(choices=[('CREATED', 'Запись СМК создана'), ('TASK_CREATED', 'Задача по мероприятию создана'), ('ARCHIVED', 'Запись СМК помещена в архив'), ('EDITED', 'Запись СМК отредактирована')], max_length=40, verbose_name='Тип события')),
                ('message', models.TextField(verbose_name='Сообщение')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='Создано')),
                ('actor', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='smk_history_events', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='history_events', to='smk.smksource', verbose_name='Запись СМК')),
            ],
            options={
                'verbose_name': 'Событие истории СМК',
                'verbose_name_plural': 'События истории СМК',
                'ordering': ['-created_at', '-pk'],
            },
        ),
    ]
