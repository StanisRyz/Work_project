from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .models import ActStatus, DefectType, Operation, Priority, TaskStatus


@login_required
def index(request):
    context = {
        'active_page': 'references',
        'page_title': 'Справочники',
        'page_description': 'Справочники используются будущими актами, задачами и несоответствиями.',
        'reference_cards': [
            {'title': 'Операции', 'count': Operation.objects.count()},
            {'title': 'Виды дефектов', 'count': DefectType.objects.count()},
            {'title': 'Статусы актов', 'count': ActStatus.objects.count()},
            {'title': 'Статусы задач', 'count': TaskStatus.objects.count()},
            {'title': 'Приоритеты', 'count': Priority.objects.count()},
        ],
    }
    return render(request, 'references/index.html', context)
