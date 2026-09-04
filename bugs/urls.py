from django.urls import path

from . import views

app_name = 'bugs'

urlpatterns = [
    path('report/', views.bug_report_create, name='report'),
    path('<int:pk>/', views.bug_report_detail, name='detail'),
]
