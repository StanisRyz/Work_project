"""Read-side assembly for the folder browser.

The browser's own selectors: the breadcrumb trail and the «Недавние
документы» block. Search has its own package (`documents/search/`) with its
own selectors; the split is deliberate, so a search backend change cannot
reach the navigation and vice versa.

Nothing here writes. Mutations stay in `documents/services.py`.
"""

from django.urls import reverse

from .models import ROOT_FOLDER_LABEL
from .search import recent_documents


# How many rows the «Недавние документы» block shows. Small on purpose: it is
# a shortcut on the way into the library, not a feed.
RECENT_LIMIT = 5


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


def build_document_breadcrumbs(document):
    """The folder trail plus the document itself, as the last (current) item."""
    trail = build_breadcrumbs(document.folder)
    for crumb in trail:
        crumb['is_current'] = False
    trail.append({
        'name': document.name,
        'url': reverse('documents:document_detail', args=[document.pk]),
        'is_current': True,
    })
    return trail


def build_recent_documents(user, limit=RECENT_LIMIT):
    """The newest files this user may see, as ordinary `SearchResult` rows.

    Same shape as a search hit, so the block reuses the result card and needs
    no rendering path of its own. Shown on the Documentation root only — deeper
    pages have the folder's own listing right there.
    """
    return recent_documents(user, limit=limit)
