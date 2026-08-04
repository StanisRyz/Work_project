from django.urls import path

from . import views


app_name = 'realtime'

urlpatterns = [
    path('events/', views.realtime_events, name='events'),
]
