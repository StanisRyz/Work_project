from django.db import migrations


GUARD_PK_LIMIT = 20


def refuse_legacy_only_acts(apps, schema_editor):
    """Stop before the columns go if any act still has no `ActDefect`.

    Such an act would lose its only defect data. The legacy columns cannot tell
    which workshop it belonged to, and guessing one from the presence of an
    operation or a party number would invent business data, so the operator
    resolves those rows by hand instead.
    """
    legacy_only = apps.get_model('acts', 'Act').objects.filter(defects__isnull=True)
    count = legacy_only.count()
    if not count:
        return
    pks = list(legacy_only.order_by('pk').values_list('pk', flat=True)[:GUARD_PK_LIMIT])
    raise RuntimeError(
        f'Актов без единого ActDefect: {count} (PK: {pks}'
        f'{", …" if count > len(pks) else ""}). '
        'Удаление устаревших полей Act уничтожило бы их данные о дефекте. '
        'Запустите «manage.py audit_legacy_act_defects», обработайте эти акты '
        'и повторите миграцию.'
    )


class Migration(migrations.Migration):
    """Phase 2: the defect summary leaves `Act` for good.

    The guard above runs first, so the columns are dropped only once every act
    has its defect data in `ActDefect`. Nothing is rewritten, created or
    deleted: primary keys, history, tasks, attachments and comments are
    untouched. `Act.due_date` stays exactly as it is.
    """

    dependencies = [
        ('acts', '0023_workshop_aware_defects'),
    ]

    operations = [
        migrations.RunPython(refuse_legacy_only_acts, migrations.RunPython.noop),
        migrations.RemoveField(model_name='act', name='znp_number'),
        migrations.RemoveField(model_name='act', name='party_number'),
        migrations.RemoveField(model_name='act', name='operation'),
        migrations.RemoveField(model_name='act', name='defect_type'),
        migrations.RemoveField(model_name='act', name='description'),
    ]
