from django.urls import path

from . import views

app_name = 'plate_cutting'

urlpatterns = [
    path('', views.plate_cutting_page, name='page'),
    path('presets/', views.preset_list, name='preset_list'),
    path('presets/create/', views.preset_create, name='preset_create'),
    path('presets/<int:pk>/', views.preset_load, name='preset_load'),
    path('presets/<int:pk>/delete/', views.preset_delete, name='preset_delete'),
]
