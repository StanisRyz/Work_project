from django.urls import path

from . import views

app_name = 'protocols'

urlpatterns = [
    path('', views.protocol_list, name='list'),
    path('create/', views.protocol_create, name='create'),
    path('<int:pk>/', views.protocol_detail, name='detail'),
    path('<int:pk>/save/', views.protocol_save_draft, name='save_draft'),
    path('<int:pk>/delete/', views.protocol_delete, name='delete'),
    # Workflow transitions: POST only, one endpoint each. A GET on any of
    # them redirects to the protocol and changes nothing.
    path(
        '<int:pk>/send-for-approval/',
        views.protocol_send_for_approval,
        name='send_for_approval',
    ),
    path('<int:pk>/approve/', views.protocol_approve, name='approve'),
    path(
        '<int:pk>/return-for-revision/',
        views.protocol_return_for_revision,
        name='return_for_revision',
    ),
]
