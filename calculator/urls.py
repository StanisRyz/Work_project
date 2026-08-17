from django.urls import path

from . import views

app_name = 'calculator'

urlpatterns = [
    path('', views.calculator_page, name='page'),
    path('entries/', views.entry_list, name='entry_list'),
    path('entries/create/', views.entry_create, name='entry_create'),
    path('entries/manual/', views.entry_manual_create, name='entry_manual_create'),
    path('entries/<int:pk>/production/', views.entry_confirm_production, name='entry_production'),
    path('entries/<int:pk>/production/unlock/', views.entry_unlock_production, name='entry_production_unlock'),
    path('entries/<int:pk>/delete/', views.entry_delete, name='entry_delete'),
    path('export/', views.export_journal, name='export'),
]
