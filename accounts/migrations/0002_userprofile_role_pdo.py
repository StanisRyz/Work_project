from django.db import migrations, models


class Migration(migrations.Migration):
    """Add ПДО to the role choices. No existing profile is reassigned."""

    dependencies = [
        ('accounts', '0001_initial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(
                choices=[
                    ('otk', 'ОТК'),
                    ('ko', 'КО'),
                    ('to', 'ТО'),
                    ('pdo', 'ПДО'),
                    ('manager', 'Руководитель'),
                    ('admin', 'Администратор'),
                ],
                default='otk',
                max_length=20,
                verbose_name='Роль',
            ),
        ),
    ]
