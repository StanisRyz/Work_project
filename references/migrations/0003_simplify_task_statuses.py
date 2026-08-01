from django.db import migrations


def simplify_task_statuses(apps, schema_editor):
    TaskStatus = apps.get_model('references', 'TaskStatus')
    Task = apps.get_model('tasks', 'Task')

    in_progress, _ = TaskStatus.objects.update_or_create(
        code='IN_PROGRESS',
        defaults={'name': 'В работе', 'sort_order': 10, 'is_final': False, 'is_active': True},
    )
    completed, _ = TaskStatus.objects.update_or_create(
        code='COMPLETED',
        defaults={'name': 'Выполнено', 'sort_order': 20, 'is_final': True, 'is_active': True},
    )
    Task.objects.filter(status__code__in=['COMPLETED', 'DONE']).update(status=completed)
    Task.objects.exclude(status__code__in=['COMPLETED', 'DONE']).update(status=in_progress)
    TaskStatus.objects.exclude(code__in=['IN_PROGRESS', 'COMPLETED']).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('references', '0002_taskstatus_completed'),
        ('tasks', '0001_initial'),
    ]

    operations = [migrations.RunPython(simplify_task_statuses, migrations.RunPython.noop)]
