from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.shortcuts import render

from .navigation import get_default_landing_url


class AppLoginView(auth_views.LoginView):
    """Login that lands a normal user on `/acts/`, an administrator on the home page.

    `?next=` still wins, so a deep link kept across the login screen — including
    the Django admin — is honoured. Only the fallback target is role-aware.
    """

    template_name = 'accounts/login.html'

    def get_success_url(self):
        return self.get_redirect_url() or get_default_landing_url(self.request.user)


@login_required
def placeholder(request):
    context = {
        'active_page': 'accounts',
        'page_title': 'Пользователи и роли',
        'page_description': 'В разделе подготовлена базовая инфраструктура ролей, подразделений и профилей пользователей.',
        'supported_roles': ['ОТК', 'КО', 'ТО', 'Руководитель', 'Администратор'],
    }
    return render(request, 'accounts/placeholder.html', context)
