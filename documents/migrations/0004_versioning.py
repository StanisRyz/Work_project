"""The version and history tables.

Schema only, and deliberately split from the data move that follows it:
`0005_document_initial_versions` copies each existing document's file into a
version 1 row while `Document.file` is still there to read, and
`0006_document_drop_file_fields` drops those columns afterwards. Three
migrations rather than one so the sequence is replayable and so nothing is
deleted before it has been copied.
"""


import django.db.models.deletion
import django.utils.timezone
import documents.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('documents', '0003_corporate_root'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name='document',
            name='uploaded_at',
            field=models.DateTimeField(auto_now_add=True, verbose_name='Создан'),
        ),
        migrations.AlterField(
            model_name='document',
            name='uploaded_by',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_documents', to=settings.AUTH_USER_MODEL, verbose_name='Создал'),
        ),
        migrations.CreateModel(
            name='DocumentVersion',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('file', models.FileField(upload_to=documents.models.document_version_upload_to, verbose_name='Файл')),
                ('number', models.PositiveIntegerField(default=1, verbose_name='Номер версии')),
                ('original_name', models.CharField(max_length=255, verbose_name='Исходное имя файла')),
                ('file_size', models.PositiveBigIntegerField(default=0, verbose_name='Размер файла')),
                ('content_type', models.CharField(blank=True, max_length=120, verbose_name='Тип содержимого')),
                ('comment', models.TextField(blank=True, verbose_name='Комментарий к версии')),
                ('is_current', models.BooleanField(default=False, verbose_name='Текущая версия')),
                ('uploaded_at', models.DateTimeField(auto_now_add=True, verbose_name='Загружена')),
                ('document', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='versions', to='documents.document', verbose_name='Документ')),
                ('uploaded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='uploaded_document_versions', to=settings.AUTH_USER_MODEL, verbose_name='Загрузил')),
            ],
            options={
                'verbose_name': 'Версия документа',
                'verbose_name_plural': 'Версии документов',
                'ordering': ['-number', '-pk'],
            },
        ),
        migrations.CreateModel(
            name='DocumentHistoryEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('document_name', models.CharField(blank=True, max_length=255, verbose_name='Название документа')),
                ('version_number', models.PositiveIntegerField(blank=True, null=True, verbose_name='Номер версии')),
                ('action', models.CharField(choices=[('DOCUMENT_CREATED', 'Документ создан'), ('VERSION_ADDED', 'Загружена версия'), ('VERSION_RESTORED', 'Версия восстановлена'), ('DOCUMENT_DELETED', 'Документ удалён')], max_length=32, verbose_name='Событие')),
                ('description', models.TextField(blank=True, verbose_name='Описание')),
                ('created_at', models.DateTimeField(db_index=True, default=django.utils.timezone.now, verbose_name='Когда')),
                ('document', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='history', to='documents.document', verbose_name='Документ')),
                ('user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='document_history_events', to=settings.AUTH_USER_MODEL, verbose_name='Пользователь')),
                ('version', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='history', to='documents.documentversion', verbose_name='Версия')),
            ],
            options={
                'verbose_name': 'Событие документа',
                'verbose_name_plural': 'История документов',
                'ordering': ['-created_at', '-pk'],
            },
        ),
        migrations.AddConstraint(
            model_name='documentversion',
            constraint=models.UniqueConstraint(fields=('document', 'number'), name='documents_version_unique_number_per_document'),
        ),
        migrations.AddConstraint(
            model_name='documentversion',
            constraint=models.UniqueConstraint(condition=models.Q(('is_current', True)), fields=('document',), name='documents_version_single_current'),
        ),
    ]
