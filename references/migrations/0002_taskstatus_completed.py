from django.db import migrations


def add_completed_status(apps, schema_editor):
    TaskStatus = apps.get_model('references', 'TaskStatus')
    TaskStatus.objects.update_or_create(
        code='COMPLETED',
        defaults={
            'name': 'Выполнена', 'description': 'Общая задача завершена одним из исполнителей.',
            'sort_order': 20, 'is_final': True, 'is_active': True,
        },
    )


class Migration(migrations.Migration):
    dependencies = [('references', '0001_initial')]
    operations = [migrations.RunPython(add_completed_status, migrations.RunPython.noop)]
