from django.shortcuts import render


def task_list(request):
    context = {
        'active_page': 'tasks',
        'page_title': 'Задачи и мероприятия',
        'page_description': 'Раздел будет использоваться для контроля задач по актам и протоколам.',
    }
    return render(request, 'tasks/list.html', context)

# Create your views here.
