from django.urls import path

from . import views

app_name = 'plate_cutting'

urlpatterns = [
    path('', views.plate_cutting_page, name='page'),
]
