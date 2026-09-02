"""The documentation file browser.

One page — `documents:browse` — plus the small POST endpoints its buttons
submit to and the download that streams a file. Thin by design: a view parses
the request, asks `documents/permissions.py` who is allowed, hands the result
to `documents/services.py` and renders.

Two things every endpoint below does deliberately:

* the permission is checked *before* the HTTP method, so a normal user who
  types a management URL into the address bar gets a genuine 403 and not a
  «method not allowed» that hides the real answer;
* a document is never reachable by its media path. `document_download` looks
  the row up, re-asks the read rule and only then opens storage, exactly as
  act and protocol attachments do.
"""

import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from ecosystem.logging_utils import log_event

from .forms import DocumentUploadForm, FolderForm
from .models import ROOT_FOLDER_LABEL, Document, DocumentFolder
from .permissions import (
    can_delete_document,
    can_delete_folder,
    can_download_document,
    can_manage_documents,
    can_rename_folder,
    can_view_documents,
)
from .services import (
    DocumentError,
    create_folder,
    delete_document,
    delete_folder,
    rename_folder,
    upload_document,
)


logger = logging.getLogger('ecosystem.documents')


def _browse_url(folder):
    """The browse URL for a folder, or for the root when it is None."""
    if folder is None:
        return reverse('documents:browse')
    return reverse('documents:folder', args=[folder.pk])


def _breadcrumbs(folder):
    """«Документация» first, then every folder down to the current one.

    The root is a label and not a row, so it is added here rather than being
    stored: see the module docstring in `documents/models.py`.
    """
    trail = [{'name': ROOT_FOLDER_LABEL, 'url': reverse('documents:browse'), 'is_current': folder is None}]
    if folder is None:
        return trail
    chain = folder.breadcrumbs()
    for entry in chain:
        trail.append({
            'name': entry.name,
            'url': _browse_url(entry),
            'is_current': entry.pk == folder.pk,
        })
    return trail


def _require_manage(user):
    """The one gate every management endpoint passes through."""
    if not can_manage_documents(user):
        log_event(
            logger,
            'WARNING',
            'documents.access_denied',
            user_id=getattr(user, 'pk', None),
            outcome='denied',
        )
        raise PermissionDenied('Недостаточно прав для изменения документации.')


@login_required
def browse(request, folder_id=None):
    """The folder listing: breadcrumbs, subfolders, documents, and the tools."""
    if not can_view_documents(request.user):
        raise PermissionDenied('Недостаточно прав для просмотра документации.')

    folder = None
    if folder_id is not None:
        folder = get_object_or_404(DocumentFolder.objects.select_related('parent'), pk=folder_id)

    subfolders = list(
        DocumentFolder.objects.filter(parent=folder).order_by('name', 'pk')
    )
    # The root is a label, not a row, so nothing can be stored directly in it:
    # every document belongs to a real folder.
    documents = (
        list(Document.objects.filter(folder=folder).select_related('uploaded_by').order_by('name', 'pk'))
        if folder is not None
        else []
    )

    can_manage = can_manage_documents(request.user)
    context = {
        'active_page': 'documents',
        'page_title': folder.name if folder else ROOT_FOLDER_LABEL,
        'header_title': ROOT_FOLDER_LABEL,
        'folder': folder,
        'parent_url': _browse_url(folder.parent) if folder is not None else None,
        'breadcrumbs': _breadcrumbs(folder),
        'subfolders': subfolders,
        'documents': documents,
        'can_manage': can_manage,
        'can_rename_current': folder is not None and can_rename_folder(folder, request.user),
        'can_delete_current': folder is not None and can_delete_folder(folder, request.user),
        # Only a real folder can receive files; at the root the toolbar offers
        # folder creation alone.
        'can_upload_here': can_manage and folder is not None,
        'folder_form': FolderForm(),
        'upload_form': DocumentUploadForm(),
    }
    return render(request, 'documents/browse.html', context)


@login_required
def folder_create(request, folder_id=None):
    """Create a subfolder of `folder_id`, or a top-level one without it."""
    _require_manage(request.user)
    parent = (
        get_object_or_404(DocumentFolder, pk=folder_id) if folder_id is not None else None
    )
    if request.method != 'POST':
        return redirect(_browse_url(parent))

    form = FolderForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors['name'][0])
        return redirect(_browse_url(parent))
    try:
        folder = create_folder(parent, form.cleaned_data['name'], request.user)
    except DocumentError as exc:
        messages.error(request, str(exc))
        return redirect(_browse_url(parent))
    messages.success(request, f'Папка «{folder.name}» создана.')
    return redirect(_browse_url(parent))


@login_required
def folder_rename(request, folder_id):
    folder = get_object_or_404(DocumentFolder.objects.select_related('parent'), pk=folder_id)
    _require_manage(request.user)
    if request.method != 'POST':
        return redirect(_browse_url(folder))

    form = FolderForm(request.POST)
    if not form.is_valid():
        messages.error(request, form.errors['name'][0])
        return redirect(_browse_url(folder))
    try:
        rename_folder(folder, form.cleaned_data['name'], request.user)
    except DocumentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Папка переименована.')
    return redirect(_browse_url(folder))


@login_required
def folder_delete(request, folder_id):
    """Delete a folder with its subfolders and files, then open its parent."""
    folder = get_object_or_404(DocumentFolder.objects.select_related('parent'), pk=folder_id)
    _require_manage(request.user)
    if request.method != 'POST':
        return redirect(_browse_url(folder))

    try:
        parent = delete_folder(folder, request.user)
    except DocumentError as exc:
        messages.error(request, str(exc))
        return redirect(_browse_url(folder))
    messages.success(request, 'Папка удалена.')
    return redirect(_browse_url(parent))


@login_required
def document_upload(request, folder_id):
    folder = get_object_or_404(DocumentFolder, pk=folder_id)
    _require_manage(request.user)
    if request.method != 'POST':
        return redirect(_browse_url(folder))

    form = DocumentUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        first_error = next(iter(form.errors.values()))[0]
        messages.error(request, first_error)
        return redirect(_browse_url(folder))
    try:
        document = upload_document(
            folder,
            form.cleaned_data['file'],
            request.user,
            name=form.cleaned_data.get('name', ''),
        )
    except DocumentError as exc:
        messages.error(request, str(exc))
        return redirect(_browse_url(folder))
    messages.success(request, f'Документ «{document.name}» загружен.')
    return redirect(_browse_url(folder))


@login_required
def document_download(request, document_id):
    """Stream one document after re-checking who may read the library.

    A refusal and a missing file are the same 404 to the client; the
    difference goes to the log, as identifiers only.
    """
    document = get_object_or_404(
        Document.objects.select_related('folder', 'uploaded_by'), pk=document_id
    )
    if not can_download_document(document, request.user):
        log_event(
            logger,
            'WARNING',
            'documents.access_denied',
            document_id=document.pk,
            user_id=getattr(request.user, 'pk', None),
            operation='download',
            outcome='denied',
        )
        raise Http404('No Document matches the given query.')
    if not document.file:
        raise Http404('Document file is missing.')
    try:
        handle = document.file.open('rb')
    except OSError as exc:
        log_event(
            logger,
            'ERROR',
            'documents.storage_failed',
            document_id=document.pk,
            user_id=getattr(request.user, 'pk', None),
            operation='download',
            error_type=type(exc).__name__,
            outcome='failed',
            exc_info=True,
        )
        raise Http404('Document file is missing.') from exc

    log_event(
        logger,
        'INFO',
        'documents.downloaded',
        document_id=document.pk,
        folder_id=document.folder_id,
        user_id=getattr(request.user, 'pk', None),
        size_bytes=document.file_size,
        operation='download',
        outcome='ok',
    )
    return FileResponse(
        handle,
        as_attachment=True,
        filename=document.original_name or document.name,
        content_type=document.content_type or 'application/octet-stream',
    )


@login_required
def document_delete(request, document_id):
    document = get_object_or_404(Document.objects.select_related('folder'), pk=document_id)
    _require_manage(request.user)
    folder = document.folder
    if request.method != 'POST':
        return redirect(_browse_url(folder))

    if not can_delete_document(document, request.user):
        raise PermissionDenied('Недостаточно прав для удаления документа.')
    try:
        delete_document(document, request.user)
    except DocumentError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, 'Документ удалён.')
    return redirect(_browse_url(folder))
