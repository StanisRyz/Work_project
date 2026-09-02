from django.urls import path

from . import views

app_name = 'smk'

urlpatterns = [
    path('create/', views.smk_create, name='create'),
    path('<int:pk>/', views.smk_detail, name='detail'),
]
