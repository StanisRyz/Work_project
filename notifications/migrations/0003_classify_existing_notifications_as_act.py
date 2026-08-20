"""Step 2 of 4: state explicitly that every existing notification is an act one.

The column default already produced this value, but the classification is a
business statement and belongs in the history as one rather than as a side
effect of a schema default — the next migrations relax `related_act` and add
the shape constraint, and both rely on this being true of every stored row.

Rows are only *updated in place*. No notification is recreated, no primary key
moves, and nothing touches recipients, read state, `read_at`, deduplication
keys, `created_at` or the `NotificationDelivery` rows hanging off them.
"""

from django.db import migrations


def classify_as_act(apps, schema_editor):
    Notification = apps.get_model('notifications', 'Notification')
    Notification.objects.exclude(source_type='ACT').update(source_type='ACT')


def unclassify(apps, schema_editor):
    # Reversing this migration only removes a statement, never data: the
    # column itself disappears with 0002.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0002_notification_source_fields'),
    ]

    operations = [
        migrations.RunPython(classify_as_act, unclassify),
    ]
