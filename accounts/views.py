from django.contrib.auth.decorators import login_required
from django.shortcuts import render


@login_required
def placeholder(request):
    context = {
        'active_page': 'accounts',
        'page_title': 'Пользователи и роли',
        'page_description': 'В разделе подготовлена базовая инфраструктура ролей, подразделений и профилей пользователей.',
        'supported_roles': ['ОТК', 'КО', 'ТО', 'Руководитель', 'Администратор'],
    }
    return render(request, 'accounts/placeholder.html', context)
