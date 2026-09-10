"""The Руководство organisational unit.

The one unit named by a role that was never created by a migration. Its role
has existed since `0001_initial`, but the department behind it was only ever
written by `seed_demo_accounts` — and that command refuses to run outside
development, so a real installation ended up with the role «Руководитель» and
nowhere to file the people holding it. Nobody could be added to Руководство
because Руководство did not exist.

Idempotent and non-destructive, exactly like `0003_pdo_department`,
`0005_mas_department`, `0007_smk_department` and `0008_organisational_departments`:
`code` is unique, so an installation that already has the row — including a
locally renamed one — keeps it untouched.

ОТК, КО and ТО are deliberately *not* created here even though the demo seeder
writes them too. They are already present on real installations, entered by
hand with their own full names, and this migration cannot see what `code` was
given to them: matching on a code that turned out different would add a second
«ОТК» beside the real one and put both into every подразделение selector. A
unit that already exists is not worth that risk; Руководство is the one that
genuinely does not.

Nobody is assigned here. Membership is an Admin decision, and it grants
nothing anyway — every permission in the project reads the role, never the
department. The single queryset that does read a department code is
`tasks.services.get_pdo_recipients()`, and it names ПДО, never this unit.
"""

from django.db import migrations

MANAGEMENT_DEPARTMENT_CODE = 'MANAGEMENT'
MANAGEMENT_DEPARTMENT_NAME = 'Руководство'


def create_management_department(apps, schema_editor):
    Department = apps.get_model('accounts', 'Department')
    Department.objects.get_or_create(
        code=MANAGEMENT_DEPARTMENT_CODE,
        defaults={'name': MANAGEMENT_DEPARTMENT_NAME, 'is_active': True},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0010_userprofile_department_roles'),
    ]

    operations = [
        # Reversing leaves the department in place: it may already carry
        # profiles, tasks or СМК records, and an organisational unit is not
        # something a schema rollback should silently destroy.
        migrations.RunPython(create_management_department, migrations.RunPython.noop),
    ]
