"""Inspection of the documentation library from Admin.

Folders stay editable here: the tree is reference-like structure, and an
administrator occasionally has to fix a system folder's name, which the page
deliberately refuses. Documents are read-only — a file is created and removed
through `documents/services.py`, which also cleans up storage, and an Admin
delete would leave the blob behind.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import Document, DocumentFolder


@admin.register(DocumentFolder)
class DocumentFolderAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'code', 'is_system', 'created_by', 'created_at', 'updated_at')
    list_filter = ('is_system',)
    search_fields = ('name', 'code')
    ordering = ('parent__name', 'name')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('parent', 'created_by')


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'folder', 'file_size', 'uploaded_by', 'uploaded_at', 'updated_at')
    list_filter = ('folder',)
    search_fields = ('name', 'original_name')
    ordering = ('-uploaded_at',)
    list_select_related = ('folder', 'uploaded_by')
