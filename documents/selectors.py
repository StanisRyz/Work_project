"""Read-side assembly for the documentation library.

The folder tree, a folder's table, the library home, the document page's side
panel and «Корзина». Search has its own package (`documents/search/`); the
split is deliberate, so a search backend change cannot reach the navigation.

Nothing here writes — mutations stay in `documents/services.py` — and nothing
here decides visibility on its own: every listing is narrowed by
`permissions.visible_folder_ids()`, the listing-side twin of
`can_view_folder()`.
"""

from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile
from tasks.models import Task

from .cards import corporate_card, file_icon
from .models import (
    ROOT_FOLDER_LABEL,
    TRASH_RETENTION_DAYS,
    Document,
    DocumentFavorite,
    DocumentFolder,
    DocumentLink,
    DocumentSubscription,
    DocumentVersion,
    DocumentVersionApproval,
)
from .permissions import (
    can_delete_folder,
    can_edit_document,
    can_manage_documents,
    can_rename_folder,
    can_set_folder_access,
    visible_folder_ids,
)
from .search import recent_documents


RECENT_LIMIT = 8
FAVORITES_LIMIT = 8
REVIEW_SOON_DAYS = 30

# The «Тип» filter: what a person means by a kind of file, not a MIME type.
TYPE_FILTERS = (
    ('pdf', 'PDF', {'pdf'}),
    ('word', 'Word', {'doc', 'docx', 'odt', 'rtf'}),
    ('excel', 'Excel', {'xls', 'xlsx', 'ods', 'csv'}),
    ('image', 'Изображения', {'png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'tif', 'tiff'}),
    ('other', 'Прочие', None),
)
_KNOWN_EXTENSIONS = set().union(*(extensions for _key, _label, extensions in TYPE_FILTERS if extensions))

STATUS_VARIANTS = {
    Document.Status.DRAFT: 'created',
    Document.Status.ACTIVE: 'completed',
    Document.Status.CANCELLED: 'archived',
}

SORT_FIELDS = {
    'name': ('name', 'pk'),
    'designation': ('designation', 'name', 'pk'),
    'status': ('status', 'name', 'pk'),
    'date': ('updated_at', 'pk'),
    'owner': ('owner_department__name', 'name', 'pk'),
}


def type_key(extension):
    extension = (extension or '').lower()
    for key, _label, extensions in TYPE_FILTERS:
        if extensions and extension in extensions:
            return key
    return 'other'


def extensions_for_type(key):
    """The extensions a «Тип» value stands for; None for «Прочие» (= none of the known)."""
    for value, _label, extensions in TYPE_FILTERS:
        if value == key:
            return extensions
    return set()


def folder_path(folder):
    return ' / '.join([ROOT_FOLDER_LABEL, *(entry.name for entry in folder.breadcrumbs())])


def build_breadcrumbs(folder):
    """«Документация / Корпоративные документы / Инструкции», every item a link.

    The current folder is a link to itself rather than plain text: the path is
    the way back out of the tree. `is_current` is still set, so the template
    marks it `aria-current`.
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
        'name': document.title,
        'url': reverse('documents:document_detail', args=[document.pk]),
        'is_current': True,
    })
    return trail


# ---------------------------------------------------------------------------
# The tree on the left
# ---------------------------------------------------------------------------


def build_folder_tree(user, current_folder=None):
    """Every folder this user may open, nested, with the path to the current
    one expanded and a document count per folder. Two queries."""
    visible = visible_folder_ids(user)
    folders = list(
        DocumentFolder.objects.filter(pk__in=visible).order_by('name', 'pk')
        .values('pk', 'name', 'parent_id', 'allowed_roles')
    )
    counts = dict(
        Document.objects.filter(folder_id__in=visible).values('folder_id')
        .annotate(total=Count('pk')).values_list('folder_id', 'total')
    )
    open_ids = set()
    if current_folder is not None:
        open_ids = {entry.pk for entry in current_folder.breadcrumbs()}
    nodes = {
        row['pk']: {
            'pk': row['pk'],
            'name': row['name'],
            'url': reverse('documents:folder', args=[row['pk']]),
            'count': counts.get(row['pk'], 0),
            'is_restricted': bool(row['allowed_roles']),
            'is_current': current_folder is not None and row['pk'] == current_folder.pk,
            # The top level is always unfolded: it is the library's table of
            # contents, and one click to see it would be one click too many.
            'is_open': row['pk'] in open_ids or row['parent_id'] is None,
            'children': [],
        }
        for row in folders
    }
    roots = []
    for row in folders:
        node = nodes[row['pk']]
        parent = nodes.get(row['parent_id'])
        (parent['children'] if parent else roots).append(node)
    return roots


# ---------------------------------------------------------------------------
# A folder's table
# ---------------------------------------------------------------------------


def _normalise_filters(params):
    status = params.get('status') or ''
    if status not in Document.Status.values:
        status = ''
    file_type = params.get('type') or ''
    if file_type not in {key for key, _label, _ext in TYPE_FILTERS}:
        file_type = ''
    sort = params.get('sort') or 'name'
    if sort.lstrip('-') not in SORT_FIELDS:
        sort = 'name'
    return {
        'q': (params.get('q') or '').strip(),
        'status': status,
        'type': file_type,
        'sort': sort,
    }


def _document_row(document, user, favorites):
    version = document.current_version
    extension = version.extension if version is not None else ''
    return {
        'document': document,
        'version': version,
        'icon': file_icon(version.original_name if version is not None else document.name),
        'type_key': type_key(extension),
        'status_variant': STATUS_VARIANTS.get(document.status, ''),
        'url': reverse('documents:document_detail', args=[document.pk]),
        'download_url': reverse('documents:document_download', args=[document.pk]) if version else '',
        'is_favorite': document.pk in favorites,
        'can_edit': can_edit_document(document, user),
        'review_due': bool(
            document.review_date and document.review_date <= timezone.localdate() + timedelta(days=REVIEW_SOON_DAYS)
        ),
    }


def build_folder_listing(folder, user, params):
    """Subfolders and documents of one folder, filtered and sorted.

    Filters are server-side GET parameters — `q`, `status`, `type`, `sort` —
    so a filtered view is a link that can be pasted to a colleague.
    """
    filters = _normalise_filters(params)
    visible = visible_folder_ids(user)
    subfolders = (
        DocumentFolder.objects.filter(parent=folder, pk__in=visible)
        .annotate(document_count=Count('documents', filter=Q(documents__deleted_at__isnull=True)))
        .order_by('name', 'pk')
    )
    folder_rows = [
        {
            'folder': sub,
            'url': reverse('documents:folder', args=[sub.pk]),
            'count': sub.document_count,
            'is_restricted': bool(sub.allowed_roles),
            'can_rename': can_rename_folder(sub, user),
            'can_delete': can_delete_folder(sub, user),
        }
        for sub in subfolders
    ]

    documents = (
        Document.objects.filter(folder=folder)
        .select_related('owner_department', 'responsible')
        .prefetch_related(Document.current_version_prefetch())
    )
    if filters['q']:
        documents = documents.filter(
            Q(name__icontains=filters['q']) | Q(designation__icontains=filters['q'])
        )
    if filters['status']:
        documents = documents.filter(status=filters['status'])
    field = filters['sort'].lstrip('-')
    ordering = SORT_FIELDS[field]
    if filters['sort'].startswith('-'):
        ordering = tuple(f'-{name}' for name in ordering)
    documents = list(documents.order_by(*ordering))
    favorites = DocumentFavorite.ids_for(user, documents)
    rows = [_document_row(document, user, favorites) for document in documents]
    if filters['type']:
        rows = [row for row in rows if row['type_key'] == filters['type']]
    if filters['q'] or filters['status'] or filters['type']:
        folder_rows = [row for row in folder_rows if filters['q'] and filters['q'].lower() in row['folder'].name.lower()]
    return {
        'filters': filters,
        'folder_rows': folder_rows,
        'document_rows': rows,
        'type_filters': [(key, label) for key, label, _ext in TYPE_FILTERS],
        'status_filters': Document.Status.choices,
        'is_filtered': bool(filters['q'] or filters['status'] or filters['type']),
    }


def build_move_targets(user):
    """Every folder a document may be moved into, as «Путь / к / папке»."""
    if not can_manage_documents(user):
        return []
    folders = list(DocumentFolder.objects.select_related('parent').order_by('name', 'pk'))
    return sorted(
        ({'pk': folder.pk, 'path': ' / '.join(entry.name for entry in folder.breadcrumbs())} for folder in folders),
        key=lambda row: row['path'].lower(),
    )


# ---------------------------------------------------------------------------
# The library home
# ---------------------------------------------------------------------------


def build_my_acknowledgements(user):
    """«Ждут моего ознакомления»: this user's open «Ознакомиться» entries."""
    tasks = (
        Task.objects.filter(
            source_type=Task.SourceType.DOCUMENT_ACK,
            individual_assignee=user,
            status__is_final=False,
            document_version__document__deleted_at__isnull=True,
        )
        .select_related('document_version__document')
        .order_by('due_date', 'pk')[:20]
    )
    return [
        {
            'document': task.document_version.document,
            'version': task.document_version,
            'due_date': task.due_date,
            'url': reverse('documents:document_detail', args=[task.document_version.document_id]),
        }
        for task in tasks
    ]


def build_my_approvals(user):
    """«Ждут моего согласования»: pending approval rows of this user."""
    approvals = (
        DocumentVersionApproval.objects.filter(
            user=user,
            status=DocumentVersionApproval.Status.PENDING,
            version__document__deleted_at__isnull=True,
        )
        .select_related('version__document', 'version__uploaded_by', 'task')
        .order_by('pk')[:20]
    )
    return [
        {
            'document': approval.version.document,
            'version': approval.version,
            'due_date': approval.task.due_date if approval.task_id else None,
            'url': reverse('documents:document_detail', args=[approval.version.document_id])
            + f'?version={approval.version_id}',
        }
        for approval in approvals
    ]


def build_review_soon(user):
    """«Скоро пересмотр»: documents in force whose review date is within
    `REVIEW_SOON_DAYS` (or already past). A manager sees all of them, anybody
    else the ones they are responsible for."""
    horizon = timezone.localdate() + timedelta(days=REVIEW_SOON_DAYS)
    documents = Document.objects.filter(
        status=Document.Status.ACTIVE, review_date__isnull=False, review_date__lte=horizon,
        folder_id__in=visible_folder_ids(user),
    ).select_related('responsible').order_by('review_date', 'pk')
    if not can_manage_documents(user):
        documents = documents.filter(responsible=user)
    return [
        {
            'document': document,
            'url': reverse('documents:document_detail', args=[document.pk]),
        }
        for document in documents[:20]
    ]


def build_favorite_documents(user, limit=FAVORITES_LIMIT):
    """This user's starred documents, as cards. Private, always."""
    if not getattr(user, 'is_authenticated', False):
        return []
    visible = visible_folder_ids(user)
    favorites = (
        DocumentFavorite.objects.filter(
            user=user, document__deleted_at__isnull=True, document__folder_id__in=visible,
        )
        .select_related('document', 'document__folder', 'document__folder__parent')
        .order_by('-created_at', '-pk')[:limit]
    )
    documents = [favorite.document for favorite in favorites]
    current = {
        version.document_id: version
        for version in DocumentVersion.objects.filter(document__in=documents, is_current=True)
    }
    for document in documents:
        setattr(document, 'prefetched_current_versions', [current[document.pk]] if document.pk in current else [])
    return [corporate_card(document, path=folder_path(document.folder), is_favorite=True) for document in documents]


def build_recent_documents(user, limit=RECENT_LIMIT):
    """The newest files this user may see, as ordinary cards."""
    return recent_documents(user, limit=limit)


def build_storage_summary(user):
    """What a manager needs to size the library: counts and bytes."""
    if not can_manage_documents(user):
        return None
    versions = DocumentVersion.objects.aggregate(total=Sum('file_size'), count=Count('pk'))
    return {
        'documents': Document.objects.count(),
        'versions': versions['count'] or 0,
        'bytes': versions['total'] or 0,
        'trash': Document.all_objects.filter(deleted_at__isnull=False).count(),
    }


# ---------------------------------------------------------------------------
# The document page
# ---------------------------------------------------------------------------


def build_version_rows(document, versions, selected):
    """One list for the side panel's «Версии» and the version switcher."""
    return [
        {
            'version': version,
            'label': version.full_label,
            'is_current': version.is_current,
            'is_selected': selected is not None and version.pk == selected.pk,
            'download_url': reverse('documents:document_version_download', args=[document.pk, version.pk]),
            'view_url': f"{reverse('documents:document_detail', args=[document.pk])}?version={version.pk}",
            'can_restore': (
                not version.is_current
                and version.approval_status not in (DocumentVersion.Approval.PENDING, DocumentVersion.Approval.RETURNED)
            ),
        }
        for version in versions
    ]


ACK_STATE_LABELS = {
    'COMPLETED': ('Ознакомлен', 'completed'),
    'CANCELLED': ('Отменено', 'archived'),
}


def build_ack_rows(version):
    """Who was asked to read this version, and who has."""
    if version is None:
        return []
    tasks = (
        Task.objects.filter(source_type=Task.SourceType.DOCUMENT_ACK, document_version=version)
        .select_related('individual_assignee__userprofile__department', 'status')
        .order_by('status__is_final', 'individual_assignee__last_name', 'individual_assignee__first_name', 'pk')
    )
    rows = []
    for task in tasks:
        label, variant = ACK_STATE_LABELS.get(task.status.code, ('Ожидается', 'in_progress'))
        profile = getattr(task.individual_assignee, 'userprofile', None)
        rows.append({
            'user': task.individual_assignee,
            'department': profile.department.name if profile is not None and profile.department_id else '',
            'label': label,
            'variant': variant,
            'at': task.completed_at if task.status.code == 'COMPLETED' else None,
            'is_overdue': not task.status.is_final and task.due_date < timezone.localdate(),
        })
    return rows


def build_approval_rows(version):
    if version is None:
        return []
    return list(version.approvals.select_related('user').order_by('pk'))


def build_link_rows(document, user):
    """«Где используется»: the acts and protocols that cite this document."""
    from acts.permissions import get_all_visible_acts_queryset

    links = document.links.select_related('act', 'protocol__protocol_type', 'created_by').order_by('-created_at')
    readable_act_ids = set(get_all_visible_acts_queryset(user).values_list('pk', flat=True)) if links else set()
    rows = []
    for link in links:
        if link.act_id:
            if link.act_id not in readable_act_ids:
                continue
            rows.append({'label': f'Акт {link.act.number or "б/н"}', 'url': reverse('acts:detail', args=[link.act_id]), 'link': link})
        else:
            protocol = link.protocol
            rows.append({
                'label': f'Протокол {protocol.protocol_type.name} №{protocol.number}',
                'url': reverse('protocols:detail', args=[protocol.pk]),
                'link': link,
            })
    return rows


def is_subscribed(user, *, document=None, folder=None):
    if not getattr(user, 'is_authenticated', False):
        return False
    return DocumentSubscription.objects.filter(user=user, document=document, folder=folder).exists()


def ack_target_labels(version):
    """The roles and departments a version was sent to, as words."""
    if version is None:
        return []
    roles = dict(UserProfile.Role.choices)
    labels = [roles.get(role, role) for role in version.ack_roles or []]
    if version.ack_department_ids:
        labels += list(
            Department.objects.filter(pk__in=version.ack_department_ids).order_by('name').values_list('name', flat=True)
        )
    return labels


# ---------------------------------------------------------------------------
# Links on an act or a protocol
# ---------------------------------------------------------------------------


def build_document_links_for(user, *, act=None, protocol=None):
    """The documents an act or protocol cites, that this user may open."""
    links = DocumentLink.objects.filter(
        act=act, protocol=protocol, document__deleted_at__isnull=True,
        document__folder_id__in=visible_folder_ids(user),
    ).select_related('document').order_by('document__designation', 'document__name')
    return [
        {
            'link': link,
            'document': link.document,
            'url': reverse('documents:document_detail', args=[link.document_id]),
        }
        for link in links
    ]


def build_linkable_documents(user):
    """Every document in force that could be cited, grouped by folder path."""
    visible = visible_folder_ids(user)
    documents = (
        Document.objects.filter(folder_id__in=visible)
        .exclude(status=Document.Status.CANCELLED)
        .select_related('folder', 'folder__parent')
        .order_by('folder__name', 'designation', 'name')
    )
    groups = {}
    for document in documents:
        groups.setdefault(document.folder.name, []).append(document)
    return sorted(groups.items(), key=lambda item: item[0].lower())


# ---------------------------------------------------------------------------
# «Корзина»
# ---------------------------------------------------------------------------


def build_trash_rows():
    from .services import can_purge

    now = timezone.now()
    rows = []
    documents = (
        Document.all_objects.filter(deleted_at__isnull=False)
        .select_related('folder', 'deleted_by')
        .prefetch_related(Document.current_version_prefetch())
        .order_by('-deleted_at', '-pk')
    )
    for document in documents:
        version = document.current_version
        purge_at = document.deleted_at + timedelta(days=TRASH_RETENTION_DAYS)
        rows.append({
            'document': document,
            'icon': file_icon(version.original_name if version else document.name),
            'folder_path': folder_path(document.folder),
            'days_left': max(0, (purge_at - now).days),
            'is_record': not can_purge(document),
        })
    return rows


def can_set_access(folder, user):
    return can_set_folder_access(folder, user)
