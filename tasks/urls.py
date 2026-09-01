from django.urls import path

from . import views

app_name = 'tasks'

urlpatterns = [
    path('', views.task_list, name='list'),
    path('list-fragment/', views.task_list_fragment, name='list_fragment'),
    path('<int:pk>/', views.task_detail, name='detail'),
    path('<int:pk>/complete/', views.complete_task_view, name='complete'),
    path('<int:pk>/attachments/add/', views.task_add_attachment, name='add_attachment'),
    path(
        '<int:pk>/attachments/<int:attachment_id>/download/',
        views.task_download_attachment,
        name='download_attachment',
    ),
]
