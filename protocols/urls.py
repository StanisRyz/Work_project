from django.urls import path

from . import views

app_name = 'protocols'

urlpatterns = [
    path('', views.protocol_list, name='list'),
    path('create/', views.protocol_create, name='create'),
    path('<int:pk>/', views.protocol_detail, name='detail'),
    path('<int:pk>/save/', views.protocol_save_draft, name='save_draft'),
    path('<int:pk>/delete/', views.protocol_delete, name='delete'),
]
