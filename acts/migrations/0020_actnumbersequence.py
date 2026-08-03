import re

from django.db import migrations, models


# Repeated locally instead of importing from acts.models: migrations must keep
# working even if the model-level constant changes later.
ACT_NUMBER_PATTERN = re.compile(r'^АОК-(\d{4})-(\d+)$')


def initialize_sequences(apps, schema_editor):
    """Seed one sequence row per year from the existing act numbers.

    Existing `Act.number` values are never modified. Numbers that do not match
    the canonical `АОК-YYYY-NNN` form (for example the seeded `АОК-DEMO-001`)
    are ignored, so a non-standard historical number cannot break the counter.
    """
    Act = apps.get_model('acts', 'Act')
    ActNumberSequence = apps.get_model('acts', 'ActNumberSequence')

    highest_per_year = {}
    for number in Act.objects.values_list('number', flat=True).iterator():
        match = ACT_NUMBER_PATTERN.match((number or '').strip())
        if not match:
            continue
        year = int(match.group(1))
        value = int(match.group(2))
        if value > highest_per_year.get(year, 0):
            highest_per_year[year] = value

    ActNumberSequence.objects.bulk_create(
        [
            ActNumberSequence(year=year, last_value=last_value)
            for year, last_value in sorted(highest_per_year.items())
        ]
    )


def drop_sequences(apps, schema_editor):
    apps.get_model('acts', 'ActNumberSequence').objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('acts', '0019_actdefect_workshop'),
    ]

    operations = [
        migrations.CreateModel(
            name='ActNumberSequence',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('year', models.PositiveIntegerField(unique=True, verbose_name='Год')),
                ('last_value', models.PositiveIntegerField(default=0, verbose_name='Последний выданный номер')),
            ],
            options={
                'verbose_name': 'Последовательность номеров актов',
                'verbose_name_plural': 'Последовательности номеров актов',
                'ordering': ['year'],
            },
        ),
        migrations.RunPython(initialize_sequences, drop_sequences),
    ]
