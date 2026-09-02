from django.urls import path

from . import views

app_name = 'documents'

urlpatterns = [
    # The browser itself. `browse` is the «Документация» root — a label, not a
    # folder row — and `folder` is any directory inside it.
    path('', views.browse, name='browse'),
    path('folders/<int:folder_id>/', views.browse, name='folder'),

    # Management: POST only, and each one checks the permission before the
    # method, so a forbidden URL answers 403 rather than 405.
    path('folders/create/', views.folder_create, name='folder_create'),
    path('folders/<int:folder_id>/create/', views.folder_create, name='subfolder_create'),
    path('folders/<int:folder_id>/rename/', views.folder_rename, name='folder_rename'),
    path('folders/<int:folder_id>/delete/', views.folder_delete, name='folder_delete'),
    path('folders/<int:folder_id>/upload/', views.document_upload, name='document_upload'),

    # Files are served only through this view, never from a media URL.
    path('files/<int:document_id>/download/', views.document_download, name='document_download'),
    path('files/<int:document_id>/delete/', views.document_delete, name='document_delete'),
]
