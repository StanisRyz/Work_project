"""Acts already waiting for КО start their first counted round.

`Act.ko_round` was added at 0 for every act. An act in `KO_REVIEW` right now is
in a round nobody has decided yet — any decision its defects carry is from an
earlier round (it was returned from ТО) — so it is moved to round 1 while its
defects stay at 0: every defect must be decided again, exactly as the single
КО form required before. No other act is touched, no decision is rewritten,
and nothing is emitted: a migration is not a workflow transition.
"""

from django.db import migrations


def open_round(apps, schema_editor):
    Act = apps.get_model('acts', 'Act')
    Act.objects.filter(status__code='KO_REVIEW', ko_round=0).update(ko_round=1)


class Migration(migrations.Migration):

    dependencies = [
        ('acts', '0029_workshop_ko_roles'),
    ]

    operations = [
        migrations.RunPython(open_round, migrations.RunPython.noop),
    ]
