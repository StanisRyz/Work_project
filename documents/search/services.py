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

import re

from django.db.models import Q
from django.utils.html import escape
from django.utils.safestring import mark_safe

from documents.cards import corporate_card, reference_card
from documents.models import ROOT_FOLDER_LABEL, Document, DocumentFavorite, DocumentFolder, DocumentVersion
from documents.permissions import can_view_documents, can_view_system_attachments, visible_folder_ids
from documents.references import SOURCES

from .types import (
    MIN_QUERY_LENGTH,
    RESULT_LIMIT,
    SCOPE_ALL,
    SEARCH_SCOPES,
    SEARCH_SCOPE_VALUES,
)


SNIPPET_RADIUS = 90


def _folder_path(folder):
    """«Документация / Корпоративные документы / Инструкции» for one folder."""
    return ' / '.join([ROOT_FOLDER_LABEL, *(entry.name for entry in folder.breadcrumbs())])


def _corporate_result(document, favorite_ids, snippet=''):
    return corporate_card(
        document,
        path=_folder_path(document.folder),
        is_favorite=document.pk in favorite_ids,
        snippet=snippet,
    )


def _corporate_queryset():
    # The current version comes along in one extra query for the whole page,
    # because every result card needs its size, date and download URL.
    return Document.objects.select_related('folder', 'folder__parent').prefetch_related(
        Document.current_version_prefetch()
    )


def build_snippet(text, query):
    """A fragment of `text` around the first occurrence of `query`, escaped,
    with every occurrence in `<mark>`. Empty when the text does not contain it."""
    if not text or not query:
        return ''
    lowered = text.lower()
    position = lowered.find(query.lower())
    if position < 0:
        return ''
    start = max(0, position - SNIPPET_RADIUS)
    end = min(len(text), position + len(query) + SNIPPET_RADIUS)
    fragment = ' '.join(text[start:end].split())
    pattern = re.compile(re.escape(escape(query)), re.IGNORECASE)
    marked = pattern.sub(lambda match: f'<mark>{match.group(0)}</mark>', escape(fragment))
    return mark_safe(f'{"… " if start else ""}{marked}{" …" if end < len(text) else ""}')


def _subtree_ids(folder_id):
    """The folder and every folder below it (for the «Папка» filter)."""
    ids, frontier = {folder_id}, [folder_id]
    while frontier:
        frontier = list(DocumentFolder.objects.filter(parent_id__in=frontier).values_list('pk', flat=True))
        frontier = [pk for pk in frontier if pk not in ids]
        ids.update(frontier)
    return ids


def _search_corporate(user, query, limit, filters=None):
    """Corporate documents matching by name, designation, file name, folder —
    or by the words inside the current version («поиск по тексту»)."""
    if not can_view_documents(user):
        return []
    filters = filters or {}
    folder_ids = visible_folder_ids(user)
    if filters.get('folder'):
        folder_ids = folder_ids & _subtree_ids(filters['folder'])
    documents = _corporate_queryset().filter(folder_id__in=folder_ids).filter(
        Q(name__icontains=query)
        | Q(designation__icontains=query)
        # Any version's stored filename, not only the current one: a document
        # people still call by the name of an older revision has to be findable.
        | Q(versions__original_name__icontains=query)
        | Q(folder__name__icontains=query)
        # The text is searched in the version people read, never in an old or
        # an unapproved one — a hit must be in what the document says now.
        | Q(versions__is_current=True, versions__text_content__icontains=query)
    )
    if filters.get('status'):
        documents = documents.filter(status=filters['status'])
    if filters.get('date_from'):
        documents = documents.filter(updated_at__date__gte=filters['date_from'])
    if filters.get('date_to'):
        documents = documents.filter(updated_at__date__lte=filters['date_to'])
    documents = list(documents.distinct().order_by('name', 'pk')[:limit])
    if filters.get('type'):
        from documents.selectors import type_key

        documents = [document for document in documents if type_key(document.extension) == filters['type']]
    texts = dict(
        DocumentVersion.objects.filter(document__in=documents, is_current=True)
        .values_list('document_id', 'text_content')
    )
    favorites = DocumentFavorite.ids_for(user, documents)
    return [
        _corporate_result(document, favorites, snippet=build_snippet(texts.get(document.pk, ''), query))
        for document in documents
    ]


def normalise_query(raw):
    """The trimmed term, or '' when it is too short to be worth running."""
    query = (raw or '').strip()
    return query if len(query) >= MIN_QUERY_LENGTH else ''


def normalise_scope(raw):
    return raw if raw in SEARCH_SCOPE_VALUES else SCOPE_ALL


def search_documents(user, query, limit=RESULT_LIMIT, filters=None):
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

    filters = filters or {}
    results = _search_corporate(user, query, limit, filters)
    # The card filters (folder, status) describe corporate documents only; an
    # act photograph has neither, so they narrow the attachments away.
    if can_view_system_attachments(user) and not filters.get('folder') and not filters.get('status'):
        for source in SOURCES.values():
            results.extend(
                reference_card(source, reference)
                for reference in source.search(user, query, limit=limit)
                if _reference_matches(reference, filters)
            )
    return results


def _reference_matches(reference, filters):
    """The type and period filters, applied to a system attachment."""
    from documents.selectors import type_key

    if filters.get('type'):
        parts = (reference.name or '').rsplit('.', 1)
        if type_key(parts[1] if len(parts) == 2 else '') != filters['type']:
            return False
    created = reference.created_at.date() if reference.created_at else None
    if filters.get('date_from') and (created is None or created < filters['date_from']):
        return False
    if filters.get('date_to') and (created is None or created > filters['date_to']):
        return False
    return True


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
        documents = list(
            _corporate_queryset().filter(folder_id__in=visible_folder_ids(user))
            .order_by('-updated_at', '-pk')[:limit]
        )
        favorites = DocumentFavorite.ids_for(user, documents)
        results.extend(_corporate_result(document, favorites) for document in documents)
    if can_view_system_attachments(user):
        for source in SOURCES.values():
            results.extend(
                reference_card(source, reference)
                for reference in source.recent(user, limit=limit)
            )
    # One merged list, newest first. A row with no timestamp sorts last rather
    # than raising on the comparison.
    results.sort(key=lambda result: (result.created_at is not None, result.created_at), reverse=True)
    return results[:limit]
