from django.urls import path

from . import views

app_name = 'protocols'

urlpatterns = [
    path('', views.protocol_list, name='list'),
    # Live fragments: GET only, JSON, never cached. Same builders and partials
    # as the pages above, so a refreshed block matches a reload exactly.
    path('list-fragment/', views.protocol_list_fragment, name='list_fragment'),
    path('create/', views.protocol_create, name='create'),
    path('<int:pk>/', views.protocol_detail, name='detail'),
    path('<int:pk>/save/', views.protocol_save_draft, name='save_draft'),
    path('<int:pk>/heading-fragment/', views.protocol_heading_fragment, name='heading_fragment'),
    path('<int:pk>/approval-fragment/', views.protocol_approval_fragment, name='approval_fragment'),
    path('<int:pk>/content-fragment/', views.protocol_content_fragment, name='content_fragment'),
    path('<int:pk>/history-fragment/', views.protocol_history_fragment, name='history_fragment'),
    path('<int:pk>/delete/', views.protocol_delete, name='delete'),
    # The official document: the printable page and the same document as PDF.
    path('<int:pk>/print/', views.protocol_print, name='print'),
    path('<int:pk>/pdf/', views.protocol_pdf, name='pdf'),
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
