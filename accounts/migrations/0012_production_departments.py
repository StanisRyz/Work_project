"""Три производства, приходящие на смену «Мастерам производства».

Reference data and nothing else, in the shape `0008_organisational_departments`
already uses: places a person works, granting nothing. No permission, workflow
or queryset keys on any of these codes.

Deliberately *not* done here: retiring «Мастера производства» (`MAS`). Six
`PROTECT`ed foreign keys point at it — tasks, act corrective actions, protocol
decisions, protocol participants, СМК measures and the СМК record's own
«Отделение» — so it cannot be deleted while any of that history exists, and it
must not even be deactivated yet: `smk.selectors.get_editor_directory()` offers
only active departments and `SmkSourceForm` resolves a submitted one only among
active rows, so anybody still filed under an inactive unit stops being
selectable as an исполнитель anywhere, including on records that already name
them. The order is create these three, move the people in Admin, and only then
retire `MAS` — a separate, deliberate step, never a side effect of this one.

Existing tasks are not affected either way: `Task.department` is written once,
at creation, and read back never, so moving a person between units leaves every
задача they already hold saying exactly what it said.

The names deliberately echo `ActDefect.Workshop` («Цех ПиР», «Цех МП») without
being it. That is a classification of a *defect*, with its own field profiles in
`acts/workshops.py`; these are organisational units for *people*, and РЛ and ТР
have no workshop at all. The two must not be joined or kept in step.

Idempotent and non-destructive like every department migration here: `code` is
unique, so an installation that already has one of these rows — including a
locally renamed one — keeps it untouched.
"""

from django.db import migrations

# Code → name, in one place. Codes are Latin because every existing one is; the
# name is what a person reads, and it is Russian for the same reason.
DEPARTMENTS = (
    ('PROD_PIR', 'Производство ПиР'),
    ('PROD_MP_RL', 'Производство МП и РЛ'),
    ('PROD_TR', 'Производство ТР'),
)


def create_departments(apps, schema_editor):
    Department = apps.get_model('accounts', 'Department')
    for code, name in DEPARTMENTS:
        Department.objects.get_or_create(
            code=code, defaults={'name': name, 'is_active': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0011_management_department'),
    ]

    operations = [
        # Reversing leaves them in place: a unit may already carry profiles,
        # tasks or acts, and an organisational unit is not something a schema
        # rollback should silently destroy.
        migrations.RunPython(create_departments, migrations.RunPython.noop),
    ]
