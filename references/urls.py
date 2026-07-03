from django.urls import path

from . import views

app_name = 'references'

urlpatterns = [
    path('', views.index, name='index'),
]
