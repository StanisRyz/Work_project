from django.urls import path

from . import views

app_name = 'acts'

urlpatterns = [
    path('', views.act_list, name='list'),
    path('create/', views.act_create, name='create'),
    path('<int:pk>/', views.act_detail, name='detail'),
    path('<int:pk>/send-to-ko/', views.act_send_to_ko, name='send_to_ko'),
    path('<int:pk>/ko-decision/', views.act_ko_decision, name='ko_decision'),
    path('<int:pk>/to-analysis/', views.act_to_analysis, name='to_analysis'),
    path('<int:pk>/comments/add/', views.act_add_comment, name='add_comment'),
    path('<int:pk>/attachments/add/', views.act_add_attachment, name='add_attachment'),
    path(
        '<int:pk>/attachments/<int:attachment_id>/download/',
        views.act_download_attachment,
        name='download_attachment',
    ),
    path(
        '<int:pk>/attachments/<int:attachment_id>/delete/',
        views.act_delete_attachment,
        name='delete_attachment',
    ),
]
