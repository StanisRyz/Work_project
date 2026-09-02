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
from django.urls import include, path, re_path
from django.views.generic.base import RedirectView

from . import health
from .legacy_urls import legacy_prefix_alias

urlpatterns = [
    # Unauthenticated on purpose: a process manager or load balancer must be
    # able to probe these. Neither reveals anything about the infrastructure.
    path('health/live/', health.health_live, name='health_live'),
    path('health/ready/', health.health_ready, name='health_ready'),
    path('', RedirectView.as_view(pattern_name='acts:list', permanent=False)),

    # User-facing modules live under a two-level hierarchy that mirrors the
    # navigation: `/quality/<module>/` and `/calculators/<module>/`. New
    # modules follow the same convention; the app names and URL namespaces
    # behind them stay as they are.
    path('quality/acts/', include('acts.urls')),
    path('quality/tasks/', include('tasks.urls')),
    path('quality/protocols/', include('protocols.urls')),
    # СМК records live under the same hierarchy; the work they create is read
    # in «Задачи» like every other task, so they have no registry of their own.
    path('quality/smk/', include('smk.urls')),
    path('calculators/winding/', include('calculator.urls')),
    path('calculators/plate-cutting/', include('plate_cutting.urls')),

    # The documentation library is its own top-level section: one navigation
    # item that opens the file browser directly, with no submenu.
    path('documents/', include('documents.urls')),

    # Infrastructure stays outside that hierarchy.
    path('notifications/', include('notifications.urls')),
    path('accounts/', include('accounts.urls')),
    path('realtime/', include('realtime.urls')),
    path('admin/', admin.site.urls),

    # Temporary: the flat paths used before the hierarchy, method-preserving
    # and unnamed so only the canonical URLs are ever generated. See
    # `ecosystem/legacy_urls.py`.
    re_path(r'^acts/(?P<rest>.*)$', legacy_prefix_alias('/quality/acts/')),
    re_path(r'^tasks/(?P<rest>.*)$', legacy_prefix_alias('/quality/tasks/')),
    re_path(r'^calculator/(?P<rest>.*)$', legacy_prefix_alias('/calculators/winding/')),
]
