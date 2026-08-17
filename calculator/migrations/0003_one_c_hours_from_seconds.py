"""«1С» arithmetic is in seconds, so stored hours must be divided by 3600.

Workup Journal V2 shipped storing the expression's result as-is, which meant
`4.4*75*30` was recorded as 9900 «hours» instead of 2,75. Only
`one_c_hours` is touched: the expression, the primary keys and every other
journal value stay exactly as they are, and a row that never had a 1С value
is left alone.

Reversible on purpose — the inverse multiplies by 3600 — so the whole
migration can be rolled back together with the code that needs it.
"""
from django.db import migrations
from django.db.models import F

SECONDS_PER_HOUR = 3600


def to_hours(apps, schema_editor):
    WindingEntry = apps.get_model('calculator', 'WindingEntry')
    WindingEntry.objects.filter(one_c_hours__isnull=False).update(
        one_c_hours=F('one_c_hours') / SECONDS_PER_HOUR,
    )


def to_seconds(apps, schema_editor):
    WindingEntry = apps.get_model('calculator', 'WindingEntry')
    WindingEntry.objects.filter(one_c_hours__isnull=False).update(
        one_c_hours=F('one_c_hours') * SECONDS_PER_HOUR,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('calculator', '0002_workup_journal_v2'),
    ]

    operations = [
        migrations.RunPython(to_hours, to_seconds),
    ]
