from django.urls import path

from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.notification_list, name='list'),
    path('header-fragment/', views.notification_header_fragment, name='header_fragment'),
    path('<int:pk>/read/', views.mark_notification_read, name='mark_read'),
    path('read-all/', views.mark_all_notifications_read, name='mark_all_read'),
    path('mark-read-bulk/', views.mark_notifications_read_bulk, name='mark_read_bulk'),
]
