from django.urls import path

from . import views

app_name = 'boards'

urlpatterns = [
    path('', views.board_list, name='list'),
    path('create/', views.board_create, name='create'),
    path('<int:pk>/', views.board_detail, name='detail'),
    path('<int:pk>/members/', views.board_members, name='members'),
    # Every mutating route is POST only: a GET goes back to the board and
    # changes nothing. The right is asked before the method, so a typed-in
    # URL without it is a 403.
    path('<int:pk>/members/add/', views.members_add, name='members_add'),
    path('<int:pk>/members/<int:user_pk>/remove/', views.member_remove, name='member_remove'),
    path('<int:pk>/cards/create/', views.card_create, name='card_create'),
    path('<int:pk>/cards/<int:card_pk>/update/', views.card_update, name='card_update'),
    path('<int:pk>/cards/<int:card_pk>/move/', views.card_move, name='card_move'),
    path('<int:pk>/cards/<int:card_pk>/complete/', views.card_complete, name='card_complete'),
]
