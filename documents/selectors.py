"""Read-side assembly for the documentation pages.

`documents/search.py` decides what *matches*; this module decides what the
page *shows* — the parsed query, the filter chips with their counts, the
narrowed result list, and the breadcrumbs. Keeping the two apart is what lets
a future full-text or OCR backend replace the matching without touching a
view or a template.

Nothing here writes. Mutations stay in `documents/services.py`.
"""

from django.urls import reverse

from .models import ROOT_FOLDER_LABEL
from .search import (
    MIN_QUERY_LENGTH,
    SCOPE_ALL,
    SEARCH_SCOPES,
    count_by_scope,
    filter_by_scope,
    normalise_query,
    normalise_scope,
    search_documents,
)


def build_breadcrumbs(folder):
    """«Документация / Корпоративные документы / Инструкции», every item a link.

    The current folder is a link to itself rather than plain text: the path is
    the way back out of the tree, and a user who has scrolled a long listing
    reaches for it to reload the level they are on. `is_current` is still set,
    so the template can mark it `aria-current` and style it as the end of the
    trail.
    """
    trail = [{
        'name': ROOT_FOLDER_LABEL,
        'url': reverse('documents:browse'),
        'is_current': folder is None,
    }]
    if folder is None:
        return trail
    for entry in folder.breadcrumbs():
        trail.append({
            'name': entry.name,
            'url': reverse('documents:folder', args=[entry.pk]),
            'is_current': entry.pk == folder.pk,
        })
    return trail


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

    results = search_documents(user, query) if query else []
    counts = count_by_scope(results)
    visible = filter_by_scope(results, scope)

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
        'results': visible,
        'total_count': counts.get(SCOPE_ALL, 0),
        'breadcrumbs': [
            {'name': ROOT_FOLDER_LABEL, 'url': reverse('documents:browse'), 'is_current': False},
            {'name': 'Поиск', 'url': reverse('documents:search'), 'is_current': True},
        ],
    }
