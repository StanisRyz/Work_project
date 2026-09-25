"""What the search page renders.

`services.py` decides what matches; this module decides what is shown — the
parsed query, the filter chips with their counts, and the narrowed list.
Keeping the two apart is what lets a future full-text or OCR backend replace
the matching without touching a view or a template.

Nothing here writes, and nothing here decides visibility.
"""

from datetime import date

from django.urls import reverse

from documents.models import ROOT_FOLDER_LABEL, Document, DocumentFolder
from documents.permissions import visible_folder_ids

from .services import (
    count_by_scope,
    filter_by_scope,
    normalise_query,
    normalise_scope,
    search_documents,
)
from .types import MIN_QUERY_LENGTH, SCOPE_ALL, SEARCH_SCOPES


def build_search_state(user, query_params):
    """Everything the search page renders, for this user and these GET params.

    One search run: the full result set feeds the chip counts, and the chosen
    scope narrows the list that is displayed. A term shorter than
    `MIN_QUERY_LENGTH` is treated as no search at all rather than as a search
    that found nothing — the two say different things to a user.
    """
    raw_query = (query_params.get('q') or '').strip()
    query = normalise_query(raw_query)
    scope = normalise_scope(query_params.get('scope'))
    filters = parse_filters(user, query_params)

    results = search_documents(user, query, filters=filters) if query else []
    counts = count_by_scope(results)

    return {
        'query': raw_query,
        'has_query': bool(query),
        # A term that was typed but is too short: the page says so instead of
        # reporting an empty result set.
        'query_too_short': bool(raw_query) and not query,
        'min_query_length': MIN_QUERY_LENGTH,
        'scope': scope,
        'scopes': [
            {
                'value': value,
                'label': label,
                'count': counts.get(value, 0),
                'is_active': value == scope,
            }
            for value, label in SEARCH_SCOPES
        ],
        'results': filter_by_scope(results, scope),
        'filters': filters,
        'filter_values': {
            key: (value.isoformat() if isinstance(value, date) else value or '')
            for key, value in filters.items()
        },
        'has_filters': any(filters.values()),
        'folder_choices': folder_choices(user),
        'status_choices': Document.Status.choices,
        'type_choices': _type_choices(),
        'total_count': counts.get(SCOPE_ALL, 0),
        'breadcrumbs': [
            {'name': ROOT_FOLDER_LABEL, 'url': reverse('documents:browse'), 'is_current': False},
            {'name': 'Поиск', 'url': reverse('documents:search'), 'is_current': True},
        ],
    }


def _parse_date(raw):
    try:
        return date.fromisoformat((raw or '').strip())
    except ValueError:
        return None


def _type_choices():
    from documents.selectors import TYPE_FILTERS

    return [(key, label) for key, label, _extensions in TYPE_FILTERS]


def parse_filters(user, params):
    """The search's optional filters, each dropped when it is not valid."""
    folder = params.get('folder') or ''
    folder_id = int(folder) if folder.isdigit() and int(folder) in visible_folder_ids(user) else None
    status = params.get('status') or ''
    file_type = params.get('type') or ''
    return {
        'folder': folder_id,
        'status': status if status in Document.Status.values else '',
        'type': file_type if file_type in {key for key, _label in _type_choices()} else '',
        'date_from': _parse_date(params.get('date_from')),
        'date_to': _parse_date(params.get('date_to')),
    }


def folder_choices(user):
    visible = visible_folder_ids(user)
    folders = DocumentFolder.objects.filter(pk__in=visible).select_related('parent')
    return sorted(
        ((folder.pk, ' / '.join(entry.name for entry in folder.breadcrumbs())) for folder in folders),
        key=lambda item: item[1].lower(),
    )
