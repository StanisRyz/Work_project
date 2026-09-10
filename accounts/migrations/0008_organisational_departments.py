"""Five more organisational units: ОПР, ОЗК, ЛАБ, СКЛ, ФЭО.

Reference data and nothing else. Unlike `0003_pdo_department`,
`0005_mas_department` and `0007_smk_department` these do not accompany a new
`UserProfile.Role`: they are places a person works, they grant nothing, and no
permission in the project keys on a department code — rights are read from the
role. Membership stays an Admin decision, exactly as for the units already here.

Precisely «no permission», not «nothing at all»: `tasks.services.get_pdo_recipients()`
selects employees by `department__code='PDO'`, because planning a replacement
product is what that department does whatever its members' roles are. It is the
one place a department code carries meaning, and none of the units created here
is that place.

Idempotent and non-destructive in the same way: `code` is unique, so an
installation that already has one of these rows — including a locally renamed
one — keeps it.
"""

from django.db import migrations

# Code → name, in one place. Codes are Latin because every existing one is
# (`OTK`, `KO`, `TO`, `PDO`, `MAS`, `SMK`, `MANAGEMENT`); the name is what a
# person reads, and it is Russian for the same reason.
DEPARTMENTS = (
    ('OPR', 'Отдел продаж'),
    ('OZK', 'Отдел закупок'),
    ('LAB', 'Лаборатория'),
    ('SKL', 'Склад'),
    ('FEO', 'Финансово-экономический отдел'),
)


def create_departments(apps, schema_editor):
    Department = apps.get_model('accounts', 'Department')
    for code, name in DEPARTMENTS:
        Department.objects.get_or_create(
            code=code, defaults={'name': name, 'is_active': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0007_smk_department'),
    ]

    operations = [
        # Reversing leaves them in place: a unit may already carry profiles,
        # tasks or acts, and an organisational unit is not something a schema
        # rollback should silently destroy.
        migrations.RunPython(create_departments, migrations.RunPython.noop),
    ]
