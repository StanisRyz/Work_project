"""Install the new РЛ reference rows when an existing database is upgraded.

SIZE_NONCONFORMITY and OTHER are shared, existing references populated by the
original bootstrap. Only the eight new codes are migration-owned; keeping the
list frozen also makes the migration independent of later workshop changes.
"""

from django.db import migrations


RL_DEFECT_TYPES = (
    ('BURRS', 'Заусенцы'),
    ('MECHANICAL_DAMAGE', 'Механическое повреждение'),
    ('ROD_YOKE_MISALIGNMENT', 'Перекос стержней и ярм'),
    ('WAVE', 'Волна'),
    ('COATING_DELAMINATION', 'Отслоение покрытия'),
    ('RUST', 'Ржавчина'),
    ('PAINT_DEFECT', 'Дефект покраски'),
    ('MAGNETIC_PROPERTIES', 'Магнитные свойства'),
)


def seed_rl_defect_types(apps, schema_editor):
    defect_types = apps.get_model('references', 'DefectType').objects.using(
        schema_editor.connection.alias,
    )
    for code, name in RL_DEFECT_TYPES:
        defect_types.get_or_create(code=code, defaults={'name': name, 'is_active': True})


class Migration(migrations.Migration):
    dependencies = [('references', '0005_deviation_reasons')]

    # References may already be used by acts: rollback must not delete them.
    operations = [migrations.RunPython(seed_rl_defect_types, migrations.RunPython.noop)]
