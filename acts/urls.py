from django.urls import path

from . import views

app_name = 'acts'

urlpatterns = [
    path('', views.act_list, name='list'),
]
