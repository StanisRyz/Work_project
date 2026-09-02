"""Read-side assembly for the folder browser.

The browser's own selectors: the breadcrumb trail, the document cards a
listing renders, the personal blocks on the Documentation root («Избранное»,
«Недавние документы») and the archive counters an administrator sees. Search
has its own package (`documents/search/`) with its own selectors; the split is
deliberate, so a search backend change cannot reach the navigation.

Nothing here writes. Mutations stay in `documents/services.py`.
"""

from django.db.models import Count, Max, Prefetch, Sum
from django.urls import reverse

from .cards import corporate_card
from .models import (
    CURRENT_VERSION_ATTR,
    ROOT_FOLDER_LABEL,
    Document,
    DocumentFavorite,
    DocumentFolder,
    DocumentVersion,
)
from .search import recent_documents


# How many rows the personal blocks show. Small on purpose: they are shortcuts
# on the way into the library, not feeds.
RECENT_LIMIT = 5
FAVORITES_LIMIT = 8


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


def _folder_path(folder):
    return ' / '.join([ROOT_FOLDER_LABEL, *(entry.name for entry in folder.breadcrumbs())])


def build_document_cards(documents, user, path=None):
    """Turn `Document` rows into the cards every listing renders.

    `path` is passed once for a folder listing — every document in it shares
    the same location — and derived per document otherwise. The starred ids
    come back in one query for the whole page rather than one per card.
    """
    documents = list(documents)
    favorites = DocumentFavorite.ids_for(user, documents)
    return [
        corporate_card(
            document,
            path=path if path is not None else _folder_path(document.folder),
            is_favorite=document.pk in favorites,
        )
        for document in documents
    ]


def build_favorite_documents(user, limit=FAVORITES_LIMIT):
    """This user's starred documents, as cards. Private, always.

    The queryset is scoped to `user` with no parameter that could widen it, so
    one person's favourites are unreachable from another's session — that is
    the whole security story of the feature.
    """
    if not getattr(user, 'is_authenticated', False):
        return []
    favorites = (
        DocumentFavorite.objects.filter(user=user)
        .select_related('document', 'document__folder', 'document__folder__parent')
        .prefetch_related(
            Prefetch(
                'document__versions',
                queryset=DocumentVersion.objects.filter(is_current=True),
                to_attr=CURRENT_VERSION_ATTR,
            )
        )
        .order_by('-created_at', '-pk')[:limit]
    )
    # Every one of these is starred by definition, so the membership query
    # `build_document_cards()` would run is skipped.
    return [
        corporate_card(favorite.document, path=_folder_path(favorite.document.folder),
                       is_favorite=True)
        for favorite in favorites
    ]


def build_recent_documents(user, limit=RECENT_LIMIT):
    """The newest files this user may see, as ordinary cards.

    «Recently added», not «recently opened»: the project keeps no per-user
    access log, and a shortcut block is not a reason to start writing one on
    every page view. Uploads are data that already exists.
    """
    return recent_documents(user, limit=limit)


def build_archive_statistics():
    """A few counters for an administrator: the size of the archive.

    Four numbers and a date, from three aggregate queries. Deliberately not a
    dashboard, not per-user and not stored — there is nothing here to keep up
    to date.
    """
    folders = DocumentFolder.objects.aggregate(total=Count('pk'), last_update=Max('updated_at'))
    documents = Document.objects.aggregate(total=Count('pk'), last_update=Max('updated_at'))
    versions = DocumentVersion.objects.aggregate(total=Count('pk'), total_size=Sum('file_size'))
    return {
        'folder_count': folders['total'] or 0,
        'document_count': documents['total'] or 0,
        'version_count': versions['total'] or 0,
        # Every stored revision counts: the archive occupies what it occupies,
        # not what its current versions alone would.
        'total_size': versions['total_size'] or 0,
        'last_update': documents['last_update'] or folders['last_update'],
    }
