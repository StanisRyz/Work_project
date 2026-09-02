"""The documentation search layer.

Three modules, in dependency order:

* `types.py` — `SearchResult` and the scope constants. No queries.
* `services.py` — what matches a term, and what was uploaded recently.
* `selectors.py` — what the search page renders from that.

This package re-exports the public names, so callers write
`from documents.search import search_documents` and never import a submodule
directly. That is what keeps a future backend swap — full-text ranking, a PDF
text index, OCR, metadata filters — inside `services.py`.
"""

from .selectors import build_search_state
from .services import (
    count_by_scope,
    filter_by_scope,
    normalise_query,
    normalise_scope,
    recent_documents,
    search_documents,
)
from .types import (
    DOCUMENT_TYPE_LABELS,
    KIND_CORPORATE,
    KIND_SYSTEM,
    MIN_QUERY_LENGTH,
    RESULT_LIMIT,
    SCOPE_ALL,
    SCOPE_CORPORATE,
    SEARCH_SCOPES,
    SearchResult,
)


__all__ = [
    'DOCUMENT_TYPE_LABELS',
    'KIND_CORPORATE',
    'KIND_SYSTEM',
    'MIN_QUERY_LENGTH',
    'RESULT_LIMIT',
    'SCOPE_ALL',
    'SCOPE_CORPORATE',
    'SEARCH_SCOPES',
    'SearchResult',
    'build_search_state',
    'count_by_scope',
    'filter_by_scope',
    'normalise_query',
    'normalise_scope',
    'recent_documents',
    'search_documents',
]
