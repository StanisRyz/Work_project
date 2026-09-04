"""The second protocol kind: «Web-система».

Reference data, exactly like `0002_quality_protocol_type` — a kind of protocol
is a `ProtocolType` row and never a schema change, which is why `ProtocolType`
was made a model in the first place. Nothing else is needed: the creation page
renders one card per active row in `display_order`, `allocate_protocol_number()`
numbers each type independently, and every page reads
`protocol.protocol_type.name`. So «Web-система» starts its own series at №1
beside «Качество».

`display_order` 20 puts it after «Качество» (10), leaving room between them.
"""

from django.db import migrations

# Repeated locally on purpose: a migration must keep applying from zero even if
# the model-level constant is renamed later.
WEB_SYSTEM_CODE = 'WEB_SYSTEM'


def add_web_system_type(apps, schema_editor):
    """Seed the kind, keyed on its code and never on a pk."""
    ProtocolType = apps.get_model('protocols', 'ProtocolType')
    ProtocolType.objects.update_or_create(
        code=WEB_SYSTEM_CODE,
        defaults={'name': 'Web-система', 'is_active': True, 'display_order': 20},
    )


def remove_web_system_type(apps, schema_editor):
    """Reverse only while nothing uses it: a numbered protocol keeps its type."""
    ProtocolType = apps.get_model('protocols', 'ProtocolType')
    ProtocolType.objects.filter(
        code=WEB_SYSTEM_CODE, protocols__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [('protocols', '0006_protocolaction_requires_attachment')]
    operations = [migrations.RunPython(add_web_system_type, remove_web_system_type)]
