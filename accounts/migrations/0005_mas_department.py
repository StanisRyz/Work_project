from django.db import migrations


MAS_DEPARTMENT_CODE = 'MAS'
MAS_DEPARTMENT_NAME = 'Мастера производства'


def ensure_mas_department(apps, schema_editor):
    """Ensure the organisational MAS department exists without assigning users.

    Prefer the stable code. If an installation already has the exact named
    department under another code, reuse that row instead of creating a
    duplicate. Existing records are changed only as needed to establish the
    requested code, name and active state.
    """
    Department = apps.get_model('accounts', 'Department')
    departments = Department.objects.using(schema_editor.connection.alias)

    department = departments.filter(code=MAS_DEPARTMENT_CODE).first()
    if department is None:
        department = departments.filter(name=MAS_DEPARTMENT_NAME).order_by('pk').first()

    if department is None:
        departments.create(
            code=MAS_DEPARTMENT_CODE,
            name=MAS_DEPARTMENT_NAME,
            is_active=True,
        )
        return

    changed_fields = []
    if department.code != MAS_DEPARTMENT_CODE:
        department.code = MAS_DEPARTMENT_CODE
        changed_fields.append('code')
    if department.name != MAS_DEPARTMENT_NAME:
        department.name = MAS_DEPARTMENT_NAME
        changed_fields.append('name')
    if not department.is_active:
        department.is_active = True
        changed_fields.append('is_active')
    if changed_fields:
        department.save(using=schema_editor.connection.alias, update_fields=changed_fields)


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0004_userprofile_role_mas'),
    ]

    operations = [
        # Reversal is deliberately non-destructive: the row may already be in
        # use, while this migration never assigns or reassigns a profile.
        migrations.RunPython(ensure_mas_department, migrations.RunPython.noop),
    ]
