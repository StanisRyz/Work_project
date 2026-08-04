from django.urls import path

from . import views

app_name = 'tasks'

urlpatterns = [
    path('', views.task_list, name='list'),
    path('list-fragment/', views.task_list_fragment, name='list_fragment'),
    path('<int:pk>/', views.task_detail, name='detail'),
    path('<int:pk>/complete/', views.complete_task_view, name='complete'),
]
