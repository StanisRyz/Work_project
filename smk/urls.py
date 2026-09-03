from django.urls import path

from . import views

app_name = 'smk'

urlpatterns = [
    path('', views.smk_list, name='list'),
    path('create/', views.smk_create, name='create'),
    path('<int:pk>/', views.smk_detail, name='detail'),
    # Correcting a live record. The same two-step form `create` uses, and the
    # same page: only `update_smk_source()` writes, and only on a confirmed
    # POST.
    path('<int:pk>/edit/', views.smk_edit, name='edit'),
    # The one state change a record has. POST only: a GET on it is a 405 and
    # changes nothing.
    path('<int:pk>/archive/', views.smk_archive, name='archive'),
]
