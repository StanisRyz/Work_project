"""Inspection of the documentation library from Admin.

Folders stay editable here: the tree is reference-like structure, and an
administrator occasionally has to fix a system folder's name, which the page
deliberately refuses. Versions and history are read-only — a version is
created and removed through `documents/services.py`, which allocates the
number, moves `is_current` and writes the history event, and an Admin edit
would leave all three inconsistent.
"""

from django.contrib import admin

from ecosystem.admin import ReadOnlyAdminMixin

from .models import Document, DocumentFolder, DocumentHistoryEvent, DocumentVersion


@admin.register(DocumentFolder)
class DocumentFolderAdmin(admin.ModelAdmin):
    list_display = ('name', 'parent', 'code', 'is_system', 'created_by', 'created_at', 'updated_at')
    list_filter = ('is_system',)
    search_fields = ('name', 'code')
    ordering = ('parent__name', 'name')
    readonly_fields = ('created_at', 'updated_at')
    list_select_related = ('parent', 'created_by')


class DocumentVersionInline(ReadOnlyAdminMixin, admin.TabularInline):
    model = DocumentVersion
    extra = 0
    fields = ('number', 'original_name', 'file_size', 'is_current', 'uploaded_by', 'uploaded_at')
    ordering = ('-number',)


@admin.register(Document)
class DocumentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('name', 'folder', 'uploaded_by', 'uploaded_at', 'updated_at')
    list_filter = ('folder',)
    search_fields = ('name',)
    ordering = ('-uploaded_at',)
    list_select_related = ('folder', 'uploaded_by')
    inlines = (DocumentVersionInline,)


@admin.register(DocumentVersion)
class DocumentVersionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('document', 'number', 'original_name', 'file_size', 'is_current',
                    'uploaded_by', 'uploaded_at')
    list_filter = ('is_current',)
    search_fields = ('document__name', 'original_name')
    ordering = ('-uploaded_at',)
    list_select_related = ('document', 'uploaded_by')


@admin.register(DocumentHistoryEvent)
class DocumentHistoryEventAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ('created_at', 'document_name', 'action', 'version_number', 'user')
    list_filter = ('action',)
    search_fields = ('document_name', 'description')
    ordering = ('-created_at',)
    list_select_related = ('document', 'user')
