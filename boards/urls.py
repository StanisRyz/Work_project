from django.urls import path

from . import views

app_name = 'boards'

urlpatterns = [
    # No registry: the last sub-board opened, else the first board, else the
    # empty state — the left panel of every board page lists the boards.
    path('', views.board_list, name='list'),
    path('create/', views.board_create, name='create'),
    # The board itself leads to its first sub-board (or to the tab of the
    # card `?card=` names); a sub-board is the page.
    path('<int:pk>/', views.board_detail, name='detail'),
    path('<int:pk>/<int:sub_pk>/', views.sub_board_detail, name='sub_board'),
    path('<int:pk>/members/', views.board_members, name='members'),
    # The live structure, card panel and messages of one sub-board (JSON, GET
    # only), for `static/js/realtime/boards.js`.
    path('<int:pk>/<int:sub_pk>/fragment/', views.board_fragment, name='fragment'),
    # Every mutating route is POST only: a GET goes back to the board and
    # changes nothing. The right is asked before the method, so a typed-in
    # URL without it is a 403.
    path('<int:pk>/rename/', views.board_rename, name='rename'),
    path('<int:pk>/archive/', views.board_archive, name='archive'),
    path('<int:pk>/restore/', views.board_restore, name='restore'),
    path('<int:pk>/members/add/', views.members_add, name='members_add'),
    path('<int:pk>/members/<int:user_pk>/remove/', views.member_remove, name='member_remove'),
    # Sub-boards and columns: whoever manages the board.
    path('<int:pk>/<int:sub_pk>/sub-boards/create/', views.sub_board_create, name='sub_board_create'),
    path('<int:pk>/<int:sub_pk>/rename/', views.sub_board_rename, name='sub_board_rename'),
    path('<int:pk>/<int:sub_pk>/move/', views.sub_board_move, name='sub_board_move'),
    path('<int:pk>/<int:sub_pk>/delete/', views.sub_board_delete, name='sub_board_delete'),
    path('<int:pk>/<int:sub_pk>/columns/create/', views.column_create, name='column_create'),
    path(
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/rename/',
        views.column_rename, name='column_rename',
    ),
    path(
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/move/',
        views.column_move, name='column_move',
    ),
    path(
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/delete/',
        views.column_delete, name='column_delete',
    ),
    # Cards: created on a sub-board, then addressed by board and card; every
    # redirect after a POST goes to the card's own sub-board.
    path('<int:pk>/<int:sub_pk>/cards/create/', views.card_create, name='card_create'),
    path('<int:pk>/cards/<int:card_pk>/update/', views.card_update, name='card_update'),
    path('<int:pk>/cards/<int:card_pk>/move/', views.card_move, name='card_move'),
    path('<int:pk>/cards/<int:card_pk>/complete/', views.card_complete, name='card_complete'),
    path('<int:pk>/cards/<int:card_pk>/cancel/', views.card_cancel, name='card_cancel'),
    path('<int:pk>/cards/<int:card_pk>/comment/', views.card_comment, name='card_comment'),
]
