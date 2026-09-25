from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.dashboard_home, name='home'),
    # The topbar's quick search across акты, протоколы, задачи and СМК.
    path('search/', views.search, name='search'),
]
