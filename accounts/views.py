from django.shortcuts import render


def placeholder(request):
    context = {
        'active_page': 'accounts',
        'page_title': 'Пользователи и роли',
        'page_description': 'Раздел будет использоваться для настройки ролей ОТК, КО, ТО и других участников.',
    }
    return render(request, 'accounts/placeholder.html', context)

# Create your views here.
