"""The shapes the search layer speaks in.

Deliberately the only module here with no queries and no imports from the rest
of the app: a result, the kinds a result can be, and the filter scopes. A
future full-text, PDF-index or OCR backend replaces `services.py` and keeps
this file exactly as it is — which is the point of separating them.

`SearchResult` is a frozen value object, never a table. The library already
knows where every file is; a stored result row would be a copy that goes stale.
"""

from dataclasses import dataclass
from datetime import datetime

from documents.references import SOURCES


# What a result *is*. Corporate documents are the library's own rows and are
# editable by a document manager; system attachments are projections of act,
# protocol and task files and are read-only for everyone.
KIND_CORPORATE = 'corporate'
KIND_SYSTEM = 'system'

DOCUMENT_TYPE_LABELS = {
    KIND_CORPORATE: 'Корпоративный документ',
    KIND_SYSTEM: 'Системное вложение',
}

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


@dataclass(frozen=True)
class SearchResult:
    """One hit — or one recent item — whichever half of the library it is from.

    Flat and display-shaped on purpose: the card template renders this and
    never reaches back into a model, so corporate documents and system
    attachments need no separate rendering path. `kind` is the only thing the
    card branches on, and it branches on it once.
    """

    # Identity and classification.
    kind: str
    scope: str
    document_type: str
    source_label: str

    # What is shown.
    title: str
    path: str
    size: int
    created_at: datetime | None

    # Where it goes. `open_url` is the location — the folder for a corporate
    # document, the act/protocol/task for an attachment — and `open_label`
    # names it. `download_url` is empty when `can_download` is False.
    open_url: str
    open_label: str
    download_url: str
    can_download: bool

    # Stated as data rather than inferred from `kind` in a template, so the
    # rule has one source.
    is_readonly: bool
