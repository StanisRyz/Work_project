from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'accounts'

urlpatterns = [
    path('login/', views.AppLoginView.as_view(), name='login'),
    # Opened as a dialog from the profile menu; this page is the same form
    # without JavaScript, and where a rejected form comes back with its errors.
    path('password/change/', views.AppPasswordChangeView.as_view(), name='password_change'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
]
