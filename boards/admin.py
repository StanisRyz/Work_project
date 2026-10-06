"""Boards, read in Django Admin.

Read-only, like every other business record: a board, its members, its
sub-boards and columns, its card fields and its cards are written only by `boards/services.py`, which creates and changes the
card's task in the same transaction. An Admin edit would move a card or drop a
member without the task behind it knowing.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import (
    Board,
    BoardCard,
    BoardCardEvent,
    BoardCardFieldValue,
    BoardColumn,
    BoardColumnPin,
    BoardField,
    BoardFieldOption,
    BoardMember,
    SubBoard,
)


class BoardMemberInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = BoardMember
    fk_name = 'board'
    extra = 0


@admin.register(Board)
class BoardAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'name', 'code', 'department', 'owner', 'created_at')
    list_filter = ('department',)
    inlines = (BoardMemberInline,)


@admin.register(BoardMember)
class BoardMemberAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'user', 'added_by', 'added_at')


@admin.register(SubBoard)
class SubBoardAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'name', 'position', 'created_by', 'created_at')


class BoardColumnPinInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = BoardColumnPin
    extra = 0


@admin.register(BoardColumn)
class BoardColumnAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'sub_board', 'name', 'position', 'is_done', 'pinned_mode')
    list_filter = ('is_done', 'pinned_mode')
    inlines = (BoardColumnPinInline,)


@admin.register(BoardCard)
class BoardCardAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'number', 'sub_board', 'column', 'position', 'created_by', 'created_at')


@admin.register(BoardCardEvent)
class BoardCardEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'card', 'kind', 'actor', 'created_at')
    list_filter = ('kind',)


class BoardFieldOptionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = BoardFieldOption
    extra = 0


@admin.register(BoardField)
class BoardFieldAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'name', 'kind', 'position', 'show_on_tile', 'is_archived')
    list_filter = ('kind', 'is_archived')
    inlines = (BoardFieldOptionInline,)


@admin.register(BoardCardFieldValue)
class BoardCardFieldValueAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'card', 'field', 'value_text', 'value_number', 'value_date', 'option')
