from django.urls import path

from . import views

app_name = 'acts'

urlpatterns = [
    path('', views.act_list, name='list'),
    path('list-fragment/', views.act_list_fragment, name='list_fragment'),
    path('clear-all/', views.act_clear_all, name='clear_all'),
    path('create/', views.act_create, name='create'),
    path('<int:pk>/', views.act_detail, name='detail'),
    path('<int:pk>/edit/', views.act_edit, name='edit'),
    path('<int:pk>/live-summary-fragment/', views.act_live_summary_fragment, name='live_summary_fragment'),
    path('<int:pk>/history-fragment/', views.act_history_fragment, name='history_fragment'),
    path('<int:pk>/comments-fragment/', views.act_comments_fragment, name='comments_fragment'),
    path('<int:pk>/activities-fragment/', views.act_activities_fragment, name='activities_fragment'),
    path('<int:pk>/send-to-ko/', views.act_send_to_ko, name='send_to_ko'),
    path('<int:pk>/ko-decision/', views.act_ko_decision, name='ko_decision'),
    path('<int:pk>/return-to-otk/', views.act_return_to_otk, name='return_to_otk'),
    path('<int:pk>/to-analysis/', views.act_to_analysis, name='to_analysis'),
    path('<int:pk>/return-to-ko/', views.act_return_to_ko, name='return_to_ko'),
    path('<int:pk>/return-to-to/', views.act_return_to_to, name='return_to_to'),
    path('<int:pk>/approve/', views.act_approve, name='approve'),
    path('<int:pk>/close/', views.act_close, name='close'),
    path('<int:pk>/print/', views.act_print, name='print'),
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
