"""One search across both halves of the library.

Corporate documents are rows the module owns; system attachments are
references projected from `acts`, `protocols` and `tasks`. A person looking
for a file does not care which, so both are matched here and returned as one
list of `SearchResult` — a value object, not a table. Nothing is indexed,
copied or written: the search reads the same rows the browser renders, so a
result can never describe a file that no longer exists.

Visibility is not decided here either. Corporate documents follow
`can_view_documents()`, and every system source asks the module that owns it,
exactly as the browser does — so a search result set is a subset of what the
same user could have reached by clicking.

**Where a future stage plugs in.** `search_documents()` is the only entry
point and `SearchResult` is the only shape callers see. Full-text ranking, a
PDF text index, OCR output or a metadata filter would replace the two matching
functions below — `_search_corporate()` and each source's `search()` — and
keep both. That is the whole reason matching is separated from the page
assembly in `documents/selectors.py`.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q
from django.urls import reverse

from .models import Document, ROOT_FOLDER_LABEL
from .permissions import can_view_documents, can_view_system_attachments
from .references import SOURCES


# The filter chips, in display order. `all` is not a source: it is the absence
# of a restriction, and the corporate scope plus every registered attachment
# source is what it expands to.
SCOPE_ALL = 'all'
SCOPE_CORPORATE = 'corporate'

SEARCH_SCOPES = (
    (SCOPE_ALL, 'Все'),
    (SCOPE_CORPORATE, 'Корпоративные документы'),
    *((slug, source.label) for slug, source in SOURCES.items()),
)
SEARCH_SCOPE_VALUES = frozenset(value for value, _label in SEARCH_SCOPES)

# The shortest term worth running: a single character matches most of the
# library and answers nothing.
MIN_QUERY_LENGTH = 2

# Per-scope cap. A search page is a way in, not a report; without a bound one
# careless term would render every file in the company.
RESULT_LIMIT = 50


@dataclass(frozen=True)
class SearchResult:
    """One hit, whichever half of the library it came from.

    Deliberately flat and display-shaped: the template renders a card from
    this and never reaches back into a model. `kind` is what the card keys on
    ('corporate' or 'system'), `scope` is which filter the hit belongs to, and
    `is_readonly` states the rule rather than leaving a template to infer it.
    """

    kind: str
    scope: str
    source_label: str
    name: str
    path: str
    url: str
    open_url: str
    open_label: str
    size: int
    created_at: datetime | None
    is_readonly: bool


def _folder_path(folder):
    """«Документация / Корпоративные документы / Инструкции» for one folder."""
    return ' / '.join([ROOT_FOLDER_LABEL, *(entry.name for entry in folder.breadcrumbs())])


def _search_corporate(user, query, limit):
    """Corporate documents matching by file name or by the folder holding them."""
    if not can_view_documents(user):
        return []
    documents = (
        Document.objects.filter(
            Q(name__icontains=query)
            | Q(original_name__icontains=query)
            | Q(folder__name__icontains=query)
        )
        .select_related('folder', 'folder__parent')
        .order_by('name', 'pk')[:limit]
    )
    return [
        SearchResult(
            kind='corporate',
            scope=SCOPE_CORPORATE,
            source_label='Корпоративные документы',
            name=document.name,
            path=_folder_path(document.folder),
            url=reverse('documents:document_download', args=[document.pk]),
            open_url=reverse('documents:folder', args=[document.folder_id]),
            open_label=document.folder.name,
            size=document.file_size,
            created_at=document.updated_at,
            is_readonly=False,
        )
        for document in documents
    ]


def _search_source(source, user, query, limit):
    """One attachment source's hits, mapped onto the shared result shape."""
    return [
        SearchResult(
            kind='system',
            scope=source.slug,
            source_label=source.label,
            name=reference.name,
            path=f'Вложения / {source.label} / {reference.object_label}',
            url=reference.download_url,
            open_url=reference.object_url,
            open_label=reference.object_label,
            size=reference.size,
            created_at=reference.created_at,
            is_readonly=True,
        )
        for reference in source.search(user, query, limit=limit)
    ]


def normalise_query(raw):
    """The trimmed term, or '' when it is too short to be worth running."""
    query = (raw or '').strip()
    return query if len(query) >= MIN_QUERY_LENGTH else ''


def normalise_scope(raw):
    return raw if raw in SEARCH_SCOPE_VALUES else SCOPE_ALL


def search_documents(user, query, limit=RESULT_LIMIT):
    """Every hit for `query` this user may see, across every scope.

    Always unscoped: the filter chips have to show a count each, and running
    the whole search once and narrowing it in Python is both simpler and
    cheaper than re-running it per chip. `filter_by_scope()` narrows the list
    for display.

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
            results.extend(_search_source(source, user, query, limit))
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
