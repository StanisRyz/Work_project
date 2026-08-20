from django.db import migrations


# Repeated locally on purpose: a migration must keep applying from zero even if
# the model-level constant is renamed later.
QUALITY_CODE = 'QUALITY'


def add_quality_type(apps, schema_editor):
    """Seed the first protocol kind, keyed on its code and never on a pk."""
    ProtocolType = apps.get_model('protocols', 'ProtocolType')
    ProtocolType.objects.update_or_create(
        code=QUALITY_CODE,
        defaults={'name': 'Качество', 'is_active': True, 'display_order': 10},
    )


def remove_quality_type(apps, schema_editor):
    """Reverse only while nothing uses it: a numbered protocol keeps its type."""
    ProtocolType = apps.get_model('protocols', 'ProtocolType')
    ProtocolType.objects.filter(code=QUALITY_CODE, protocols__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [('protocols', '0001_initial')]
    operations = [migrations.RunPython(add_quality_type, remove_quality_type)]
