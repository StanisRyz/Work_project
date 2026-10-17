from django.urls import path

from . import views

app_name = 'disposal'

urlpatterns = [
    path('', views.disposal_list, name='list'),
    # «Утилизировать»: POST only — the print form first, then the confirmed write.
    path('confirm/', views.disposal_confirm, name='confirm'),
    path('<int:pk>/print/', views.disposal_print, name='print'),
]
