"""Five more `UserProfile.Role` values: ОПР, ОЗК, ЛАБ, СКЛ, ФЭО.

A `choices` change and nothing else: no column is altered, no row is touched,
and every existing role keeps its stored value. It is here only because Django
tracks `choices` in migration state.

The roles grant nothing. No permission module gained a branch for them — they
read what any authenticated user reads and complete the tasks assigned to them,
which is `TaskAssignee`'s answer rather than a role's.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0009_userprofile_is_bug_responsible'),
    ]

    operations = [
        migrations.AlterField(
            model_name='userprofile',
            name='role',
            field=models.CharField(choices=[('otk', 'ОТК'), ('ko', 'КО'), ('to', 'ТО'), ('pdo', 'ПДО'), ('mas', 'Мастер производства'), ('smk', 'СМК'), ('opr', 'Отдел продаж'), ('ozk', 'Отдел закупок'), ('lab', 'Лаборатория'), ('skl', 'Склад'), ('feo', 'ФЭО'), ('manager', 'Руководитель'), ('admin', 'Администратор')], default='otk', max_length=20, verbose_name='Роль'),
        ),
    ]
