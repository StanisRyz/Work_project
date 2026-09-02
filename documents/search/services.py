"""What matches a search term, and what was added recently.

The one place matching happens. Corporate documents are queried here; system
attachments are asked of the source adapters in `documents/references.py`,
which delegate visibility and identity to `acts`, `protocols` and `tasks`. Both
come back as `SearchResult`, so the caller — and the template — never learns
which table a file lives in.

Nothing is indexed, copied or written. The search reads the same rows the
browser renders, so a result can never describe a file that no longer exists.

Visibility is not decided here. Corporate documents follow
`can_view_documents()`; every system source asks the module that owns it. A
result set is therefore always a subset of what the same user could have
reached by clicking, and search grants nothing on its own.

**Where a future stage plugs in.** `search_documents()` is the only entry
point and `SearchResult` the only shape callers see. Full-text ranking, a PDF
text index, OCR output or a metadata filter replaces `_search_corporate()` and
each source's `search()`, and keeps both.
"""

from django.db.models import Q
from django.urls import reverse

from documents.models import ROOT_FOLDER_LABEL, Document
from documents.permissions import can_view_documents, can_view_system_attachments
from documents.references import SOURCES

from .types import (
    DOCUMENT_TYPE_LABELS,
    KIND_CORPORATE,
    KIND_SYSTEM,
    MIN_QUERY_LENGTH,
    RESULT_LIMIT,
    SCOPE_ALL,
    SCOPE_CORPORATE,
    SEARCH_SCOPES,
    SEARCH_SCOPE_VALUES,
    SearchResult,
)


def _folder_path(folder):
    """«Документация / Корпоративные документы / Инструкции» for one folder."""
    return ' / '.join([ROOT_FOLDER_LABEL, *(entry.name for entry in folder.breadcrumbs())])


def _corporate_result(document):
    # A document always has a current version — `upload_document()` creates
    # both together — but a row whose version was somehow removed still has to
    # render, so the card degrades to «no download» rather than raising.
    version = document.current_version
    return SearchResult(
        kind=KIND_CORPORATE,
        scope=SCOPE_CORPORATE,
        document_type=DOCUMENT_TYPE_LABELS[KIND_CORPORATE],
        source_label='Корпоративные документы',
        title=document.name,
        path=_folder_path(document.folder),
        size=version.file_size if version is not None else 0,
        # When the *current version* was uploaded, not when the document row
        # was created: a card says how fresh the file is.
        created_at=version.uploaded_at if version is not None else document.uploaded_at,
        version_label=version.label if version is not None else '',
        open_url=reverse('documents:document_detail', args=[document.pk]),
        open_label=document.name,
        open_action_label='Открыть документ',
        download_url=(
            reverse('documents:document_download', args=[document.pk])
            if version is not None
            else ''
        ),
        can_download=version is not None,
        is_readonly=False,
    )


def _system_result(source, reference):
    return SearchResult(
        kind=KIND_SYSTEM,
        scope=source.slug,
        document_type=DOCUMENT_TYPE_LABELS[KIND_SYSTEM],
        source_label=source.label,
        title=reference.name,
        path=f'Вложения / {source.label} / {reference.object_label}',
        size=reference.size,
        created_at=reference.created_at,
        # System attachments have no versions, by design.
        version_label='',
        open_url=reference.object_url,
        open_label=reference.object_label,
        open_action_label='Открыть источник',
        download_url=reference.download_url,
        can_download=True,
        is_readonly=True,
    )


def _corporate_queryset():
    # The current version comes along in one extra query for the whole page,
    # because every result card needs its size, date and download URL.
    return Document.objects.select_related('folder', 'folder__parent').prefetch_related(
        Document.current_version_prefetch()
    )


def _search_corporate(user, query, limit):
    """Corporate documents matching by file name or by the folder holding them."""
    if not can_view_documents(user):
        return []
    documents = _corporate_queryset().filter(
        Q(name__icontains=query)
        # Any version's stored filename, not only the current one: a document
        # people still call by the name of an older revision has to be findable.
        | Q(versions__original_name__icontains=query)
        | Q(folder__name__icontains=query)
    ).distinct().order_by('name', 'pk')[:limit]
    return [_corporate_result(document) for document in documents]


def normalise_query(raw):
    """The trimmed term, or '' when it is too short to be worth running."""
    query = (raw or '').strip()
    return query if len(query) >= MIN_QUERY_LENGTH else ''


def normalise_scope(raw):
    return raw if raw in SEARCH_SCOPE_VALUES else SCOPE_ALL


def search_documents(user, query, limit=RESULT_LIMIT):
    """Every hit for `query` this user may see, across every scope.

    Always unscoped: each filter chip has to show a count, and running the
    search once and narrowing it in Python is both simpler and cheaper than
    re-running it per chip. `filter_by_scope()` narrows the list for display.

    Corporate documents come first because they are the library's own content;
    system attachments follow, in registry order. The order is deliberately
    stable and is *not* a relevance ranking — there is none yet, and implying
    one would be misleading.
    """
    query = normalise_query(query)
    if not query:
        return []

    results = _search_corporate(user, query, limit)
    if can_view_system_attachments(user):
        for source in SOURCES.values():
            results.extend(
                _system_result(source, reference)
                for reference in source.search(user, query, limit=limit)
            )
    return results


def filter_by_scope(results, scope):
    """The hits one filter chip shows. `all` narrows nothing."""
    scope = normalise_scope(scope)
    if scope == SCOPE_ALL:
        return list(results)
    return [result for result in results if result.scope == scope]


def count_by_scope(results):
    """How many hits each filter chip would show, for the current term."""
    counts = {value: 0 for value, _label in SEARCH_SCOPES}
    counts[SCOPE_ALL] = len(results)
    for result in results:
        counts[result.scope] = counts.get(result.scope, 0) + 1
    return counts


def recent_documents(user, limit=5):
    """The newest files this user may see, from both halves of the library.

    Deliberately «recently added» and not «recently opened»: the project keeps
    no per-user access log, and inventing one for a sidebar block would be a
    tracking system nobody asked for. Uploads are data that already exists.

    The same `SearchResult` the search page renders, so the block reuses the
    result card unchanged.
    """
    results = []
    if can_view_documents(user):
        # `updated_at` and not `uploaded_at`: `add_document_version()` touches
        # it, so a document that just received a new revision counts as recent.
        documents = _corporate_queryset().order_by('-updated_at', '-pk')[:limit]
        results.extend(_corporate_result(document) for document in documents)
    if can_view_system_attachments(user):
        for source in SOURCES.values():
            results.extend(
                _system_result(source, reference)
                for reference in source.recent(user, limit=limit)
            )
    # One merged list, newest first. A row with no timestamp sorts last rather
    # than raising on the comparison.
    results.sort(key=lambda result: (result.created_at is not None, result.created_at), reverse=True)
    return results[:limit]
