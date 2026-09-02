"""The scopes search speaks in.

The *result* shape is not defined here any more: a search hit is an ordinary
`documents.cards.DocumentCard` — the same object a folder listing, the
favourites block and «Недавние документы» render — so all four share one
template instead of four near-copies that drift. `SearchResult` stays as an
alias because it reads better at the call sites inside this package.

Deliberately the only module here with no queries. A future full-text,
PDF-index or OCR backend replaces `services.py` and keeps this file as it is.
"""

from documents.cards import (  # noqa: F401  (re-exported as search's vocabulary)
    DOCUMENT_TYPE_LABELS,
    KIND_CORPORATE,
    KIND_SYSTEM,
    DocumentCard as SearchResult,
)
from documents.references import SOURCES


# The filter chips, in display order. `all` is not a source — it is the
# absence of a restriction, and it expands to the corporate scope plus every
# registered attachment source.
SCOPE_ALL = 'all'
SCOPE_CORPORATE = KIND_CORPORATE

SEARCH_SCOPES = (
    (SCOPE_ALL, 'Все'),
    (SCOPE_CORPORATE, 'Корпоративные документы'),
    *((slug, source.label) for slug, source in SOURCES.items()),
)
SEARCH_SCOPE_VALUES = frozenset(value for value, _label in SEARCH_SCOPES)

# The shortest term worth running: one character matches most of the library
# and answers nothing.
MIN_QUERY_LENGTH = 2

# Per-source cap. A search page is a way in, not a report; unbounded, one
# careless term would render every file in the company.
RESULT_LIMIT = 50
