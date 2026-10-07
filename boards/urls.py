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
    # `?view=table` is the same sub-board as rows («Таблица», read only), and
    # `&export=xlsx` that table as a spreadsheet.
    path('<int:pk>/<int:sub_pk>/', views.sub_board_detail, name='sub_board'),
    path('<int:pk>/members/', views.board_members, name='members'),
    # «Отклонения»: why the board's deadlines moved (read only, `&export=xlsx`).
    path('<int:pk>/deviations/', views.board_deviations, name='deviations'),
    # The live structure, card panel and messages of one sub-board (JSON, GET
    # only), for `static/js/realtime/boards.js`.
    path('<int:pk>/<int:sub_pk>/fragment/', views.board_fragment, name='fragment'),
    # Every mutating route is POST only: a GET goes back to the board and
    # changes nothing. The right is asked before the method, so a typed-in
    # URL without it is a 403.
    path('<int:pk>/rename/', views.board_rename, name='rename'),
    path('<int:pk>/code/', views.board_change_code, name='change_code'),
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
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/pins/',
        views.column_pins, name='column_pins',
    ),
    path(
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/stale/',
        views.column_stale, name='column_stale',
    ),
    path(
        '<int:pk>/<int:sub_pk>/columns/<int:column_pk>/delete/',
        views.column_delete, name='column_delete',
    ),
    # «Поля карточек»: the board's own card fields. Read by every reader of
    # the board, changed by whoever manages it.
    path('<int:pk>/fields/', views.board_fields_page, name='fields'),
    path('<int:pk>/fields/create/', views.field_create, name='field_create'),
    path('<int:pk>/fields/<int:field_pk>/update/', views.field_update, name='field_update'),
    path('<int:pk>/fields/<int:field_pk>/move/', views.field_move, name='field_move'),
    path('<int:pk>/fields/<int:field_pk>/archive/', views.field_archive, name='field_archive'),
    path('<int:pk>/fields/<int:field_pk>/restore/', views.field_restore, name='field_restore'),
    path('<int:pk>/fields/<int:field_pk>/delete/', views.field_delete, name='field_delete'),
    path('<int:pk>/fields/<int:field_pk>/options/create/', views.option_create, name='option_create'),
    path(
        '<int:pk>/fields/<int:field_pk>/options/<int:option_pk>/update/',
        views.option_update, name='option_update',
    ),
    path(
        '<int:pk>/fields/<int:field_pk>/options/<int:option_pk>/move/',
        views.option_move, name='option_move',
    ),
    path(
        '<int:pk>/fields/<int:field_pk>/options/<int:option_pk>/archive/',
        views.option_archive, name='option_archive',
    ),
    path(
        '<int:pk>/fields/<int:field_pk>/options/<int:option_pk>/restore/',
        views.option_restore, name='option_restore',
    ),
    path(
        '<int:pk>/fields/<int:field_pk>/options/<int:option_pk>/delete/',
        views.option_delete, name='option_delete',
    ),
    # Cards: created on a sub-board, then addressed by board and card; every
    # redirect after a POST goes to the card's own sub-board.
    path('<int:pk>/<int:sub_pk>/cards/create/', views.card_create, name='card_create'),
    path('<int:pk>/cards/<int:card_pk>/update/', views.card_update, name='card_update'),
    path('<int:pk>/cards/<int:card_pk>/move/', views.card_move, name='card_move'),
    path('<int:pk>/cards/<int:card_pk>/complete/', views.card_complete, name='card_complete'),
    path('<int:pk>/cards/<int:card_pk>/reopen/', views.card_reopen, name='card_reopen'),
    path('<int:pk>/cards/<int:card_pk>/cancel/', views.card_cancel, name='card_cancel'),
    path('<int:pk>/cards/<int:card_pk>/comment/', views.card_comment, name='card_comment'),
    # The files of «Чат»: protected — served only after reading the board is
    # asked again; an image also inline, for its thumbnail.
    path('<int:pk>/cards/<int:card_pk>/files/<int:file_pk>/', views.file_download, name='file_download'),
    path(
        '<int:pk>/cards/<int:card_pk>/files/<int:file_pk>/preview/',
        views.file_preview, name='file_preview',
    ),
    path(
        '<int:pk>/cards/<int:card_pk>/files/<int:file_pk>/delete/',
        views.file_delete, name='file_delete',
    ),
    # «Подзадачи»: whoever works on the board, an open card that is no subtask.
    path('<int:pk>/cards/<int:card_pk>/subtasks/create/', views.subtask_create, name='subtask_create'),
    path(
        '<int:pk>/cards/<int:card_pk>/subtasks/create-list/',
        views.subtask_create_list, name='subtask_create_list',
    ),
    # «Следить» / «Вы следите»: any reader of a live board.
    path('<int:pk>/cards/<int:card_pk>/subscribe/', views.card_subscribe, name='card_subscribe'),
    # The card's «Чек-лист»: whoever works on the board, an open card only.
    # The checkbox answers a `fetch` in JSON, like a drag.
    path('<int:pk>/cards/<int:card_pk>/checklist/add/', views.checklist_add, name='checklist_add'),
    path(
        '<int:pk>/cards/<int:card_pk>/checklist/<int:item_pk>/rename/',
        views.checklist_rename, name='checklist_rename',
    ),
    path(
        '<int:pk>/cards/<int:card_pk>/checklist/<int:item_pk>/toggle/',
        views.checklist_toggle, name='checklist_toggle',
    ),
    path(
        '<int:pk>/cards/<int:card_pk>/checklist/<int:item_pk>/move/',
        views.checklist_move, name='checklist_move',
    ),
    path(
        '<int:pk>/cards/<int:card_pk>/checklist/<int:item_pk>/to-subtask/',
        views.checklist_to_subtask, name='checklist_to_subtask',
    ),
    path(
        '<int:pk>/cards/<int:card_pk>/checklist/<int:item_pk>/delete/',
        views.checklist_delete, name='checklist_delete',
    ),
]
