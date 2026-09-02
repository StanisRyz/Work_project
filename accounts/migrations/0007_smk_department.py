"""The Отдел СМК organisational unit.

Idempotent and non-destructive, exactly like `0003_pdo_department` and
`0005_mas_department`: `code` is unique, so an installation that already has
the row — including a locally renamed one — keeps it. Nobody is assigned to it
here; membership is an Admin decision, and it grants nothing anyway —
`smk.permissions` reads the role, never the department.
"""

from django.db import migrations

SMK_DEPARTMENT_CODE = 'SMK'
SMK_DEPARTMENT_NAME = 'Отдел СМК'


def create_smk_department(apps, schema_editor):
    Department = apps.get_model('accounts', 'Department')
    Department.objects.get_or_create(
        code=SMK_DEPARTMENT_CODE,
        defaults={'name': SMK_DEPARTMENT_NAME, 'is_active': True},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0006_userprofile_role_smk'),
    ]

    operations = [
        # Reversing leaves the department in place: it may already carry
        # profiles or tasks, and an organisational unit is not something a
        # schema rollback should silently destroy.
        migrations.RunPython(create_smk_department, migrations.RunPython.noop),
    ]
