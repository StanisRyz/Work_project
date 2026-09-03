"""«Отменена» — the third and last task status.

A task is cancelled when the document it came out of was corrected and the
work it asked for was reissued: the row is kept, never deleted, so what was
once asked of somebody stays readable. Final, like `COMPLETED`, which is what
keeps it out of the active tabs of «Задачи» and in «Архив».
"""

from django.db import migrations


def add_cancelled_status(apps, schema_editor):
    TaskStatus = apps.get_model('references', 'TaskStatus')
    TaskStatus.objects.update_or_create(
        code='CANCELLED',
        defaults={
            'name': 'Отменена',
            'description': 'Задача отменена: исходный документ был отредактирован.',
            'sort_order': 30,
            'is_final': True,
            'is_active': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('references', '0003_simplify_task_statuses')]
    operations = [migrations.RunPython(add_cancelled_status, migrations.RunPython.noop)]
