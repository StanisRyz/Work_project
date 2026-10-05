"""Boards, read in Django Admin.

Read-only, like every other business record: a board, its members and its
cards are written only by `boards/services.py`, which creates and changes the
card's task in the same transaction. An Admin edit would move a card or drop a
member without the task behind it knowing.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import Board, BoardCard, BoardMember


class BoardMemberInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = BoardMember
    fk_name = 'board'
    extra = 0


@admin.register(Board)
class BoardAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'name', 'department', 'owner', 'created_at')
    list_filter = ('department',)
    inlines = (BoardMemberInline,)


@admin.register(BoardMember)
class BoardMemberAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'user', 'added_by', 'added_at')


@admin.register(BoardCard)
class BoardCardAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('pk', 'board', 'stage', 'position', 'created_by', 'created_at')
    list_filter = ('stage',)
