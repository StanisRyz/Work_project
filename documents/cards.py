"""The one shape every file in Documentation is rendered from.

A folder listing, a search result, the favourites block and «Недавние
документы» all show the same thing — a file, where it lives, how fresh it is
and what can be done with it — so they share one dataclass and one template
(`templates/documents/includes/document_card.html`) instead of four near-copies
that drift.

`DocumentCard` is a frozen value object built per request. It is not a table,
and it is not stored: corporate documents come from `Document` +
`DocumentVersion`, system attachments from the read-only `DocumentReference`
projections in `documents/references.py`, and the card is where those two stop
being different for the caller.

The only branch a template needs is `is_readonly`. Everything else — the icon,
the version label, whether a download is offered — is decided here.
"""

from dataclasses import dataclass
from datetime import datetime

from django.urls import reverse


# What a card *is*. Corporate documents are the library's own rows and are
# managed by a document manager; system attachments are projections of act,
# protocol and task files and are read-only for everyone, permanently.
KIND_CORPORATE = 'corporate'
KIND_SYSTEM = 'system'

DOCUMENT_TYPE_LABELS = {
    KIND_CORPORATE: 'Корпоративный документ',
    KIND_SYSTEM: 'Системное вложение',
}

# File-type icons. Emoji on purpose: no image assets to ship, cache-bust or
# collect, and they render identically wherever the rest of the module's icons
# already do (📁 for folders, 🔒 for the system branch).
ICON_DEFAULT = '📄'
ICON_SYSTEM = '🔒'
ICON_FOLDER = '📁'
EXTENSION_ICONS = {
    'pdf': '📕',
    'doc': '📘', 'docx': '📘', 'odt': '📘', 'rtf': '📘',
    'xls': '📗', 'xlsx': '📗', 'ods': '📗', 'csv': '📗',
    'ppt': '📙', 'pptx': '📙', 'odp': '📙',
    'png': '🖼️', 'jpg': '🖼️', 'jpeg': '🖼️', 'webp': '🖼️',
    'gif': '🖼️', 'bmp': '🖼️', 'tif': '🖼️', 'tiff': '🖼️',
    'txt': '📃', 'md': '📃',
}


def file_icon(filename, is_system=False):
    """The icon for one file. A system attachment is marked before its type.

    Read-only is the more important fact about an act photograph than «this is
    a JPEG», so the lock wins: a user scanning a mixed list sees at a glance
    which rows they cannot change.
    """
    if is_system:
        return ICON_SYSTEM
    parts = (filename or '').rsplit('.', 1)
    extension = parts[1].lower() if len(parts) == 2 else ''
    return EXTENSION_ICONS.get(extension, ICON_DEFAULT)


@dataclass(frozen=True)
class DocumentCard:
    """One file, described for display. Never persisted.

    Flat and template-shaped: the card renders this and never reaches back
    into a model, which is what lets corporate documents and system
    attachments share a single rendering path.
    """

    # Identity and classification.
    kind: str
    scope: str
    document_type: str
    source_label: str
    icon: str

    # What is shown.
    title: str
    path: str
    size: int
    created_at: datetime | None
    # «v3» for a corporate document, empty for a system attachment: those are
    # projections of another module's files and have no versions.
    version_label: str

    # Where it goes. `open_url` opens the item itself — the document page for a
    # corporate document, the owning act/protocol/task for an attachment —
    # `open_label` names that target and `open_action_label` captions the
    # button. `download_url` is empty when `can_download` is False.
    open_url: str
    open_label: str
    open_action_label: str
    download_url: str
    can_download: bool

    # Stated as data rather than inferred from `kind` in a template, so the
    # read-only rule has one source.
    is_readonly: bool

    # Favourites are corporate-only and per viewer, so this is always False for
    # a system attachment and may differ between two users looking at the same
    # document. `favorite_url` is empty when the card cannot be favourited.
    is_favorite: bool = False
    favorite_url: str = ''


def corporate_card(document, path, is_favorite=False):
    """A `Document` and its current version, as a card.

    `path` is passed in rather than derived: a folder listing already knows
    where it is and should not walk the tree once per row.

    A document always has a current version — `upload_document()` creates both
    together — but a row whose version was somehow removed still has to render,
    so the card degrades to «no download» instead of raising.
    """
    version = document.current_version
    return DocumentCard(
        kind=KIND_CORPORATE,
        scope=KIND_CORPORATE,
        document_type=DOCUMENT_TYPE_LABELS[KIND_CORPORATE],
        source_label='Корпоративные документы',
        icon=file_icon(version.original_name if version is not None else document.name),
        title=document.name,
        path=path,
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
        is_favorite=is_favorite,
        favorite_url=reverse('documents:favorite_toggle', args=[document.pk]),
    )


def reference_card(source, reference):
    """One `DocumentReference` — an act, protocol or task file — as a card.

    Read-only, never favouritable and never versioned: the file belongs to the
    record that owns it, and Documentation only points at it.
    """
    return DocumentCard(
        kind=KIND_SYSTEM,
        scope=source.slug,
        document_type=DOCUMENT_TYPE_LABELS[KIND_SYSTEM],
        source_label=source.label,
        icon=file_icon(reference.name, is_system=True),
        title=reference.name,
        path=f'Вложения / {source.label} / {reference.object_label}',
        size=reference.size,
        created_at=reference.created_at,
        version_label='',
        open_url=reference.object_url,
        open_label=reference.object_label,
        open_action_label='Открыть источник',
        download_url=reference.download_url,
        can_download=True,
        is_readonly=True,
    )
