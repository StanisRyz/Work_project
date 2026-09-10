from django.db import migrations

PDO_DEPARTMENT_CODE = 'PDO'
PDO_DEPARTMENT_NAME = 'Планово-диспетчерская служба'


def create_pdo_department(apps, schema_editor):
    """Make the department exist, in development and in production alike.

    Idempotent on purpose: `code` is unique, so an installation that already
    has the row keeps it — including a locally renamed one, which a migration
    has no business overwriting. Nobody is assigned to it here: membership is
    an Admin decision and it grants nothing anyway —
    `calculator.permissions.can_manage_workup()` reads the role, not the
    department.

    This code, however, is load-bearing and must not be «cleaned up» as unused:
    `tasks.services.get_pdo_recipients()` selects the people to notify about a
    rejection task by `department__code='PDO'` — by department rather than by
    role, on purpose. Renaming the row is safe; changing its code is not.
    """
    Department = apps.get_model('accounts', 'Department')
    Department.objects.get_or_create(
        code=PDO_DEPARTMENT_CODE,
        defaults={'name': PDO_DEPARTMENT_NAME, 'is_active': True},
    )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_userprofile_role_pdo'),
    ]

    operations = [
        # Reversing leaves the department in place: it may already carry
        # profiles or corrective actions, and an organisational unit is not
        # something a schema rollback should silently destroy.
        migrations.RunPython(create_pdo_department, migrations.RunPython.noop),
    ]
