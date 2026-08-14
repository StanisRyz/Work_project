"""
URL configuration for ecosystem project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic.base import RedirectView

from . import health

urlpatterns = [
    # Unauthenticated on purpose: a process manager or load balancer must be
    # able to probe these. Neither reveals anything about the infrastructure.
    path('health/live/', health.health_live, name='health_live'),
    path('health/ready/', health.health_ready, name='health_ready'),
    path('', RedirectView.as_view(pattern_name='acts:list', permanent=False)),
    path('acts/', include('acts.urls')),
    path('tasks/', include('tasks.urls')),
    path('notifications/', include('notifications.urls')),
    path('accounts/', include('accounts.urls')),
    path('realtime/', include('realtime.urls')),
    path('admin/', admin.site.urls),
]
