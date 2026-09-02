"""The documentation library: a folder tree and the files inside it.

Tables named for what they are and not for who uploaded them —
`DocumentFolder`, `Document`, `DocumentVersion`, `DocumentHistoryEvent`, never
`UserFile` — because the same tree is meant to grow a second branch later:

    Документация
     ├── Корпоративные документы
     └── Вложения
          ├── Акты
          ├── Протоколы
          └── Задачи

Nothing in this stage implements that branch. What it does is leave room for
it: a folder may be marked `is_system` and addressed by a stable `code`, so a
future migration can attach the attachment subtree without matching folder
names, and `Document` carries no back-reference to any one module.

«Документация» itself is not a row. It is the browse root, and the folders
created by the initial data migration sit directly under it with
`parent = NULL`; that is what keeps the future «Вложения» branch a sibling of
«Корпоративные документы» rather than something nested inside a real folder
that would have to be moved.
"""

from uuid import uuid4

from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.utils import timezone


# How deep the tree may go. Not a storage limit — a guard so a breadcrumb
# stays readable and a recursive walk stays bounded.
MAX_FOLDER_DEPTH = 10

# What the breadcrumb shows before the first real folder.
ROOT_FOLDER_LABEL = 'Документация'

# The two branches directly under that root. «Корпоративные документы» is a
# real (system) folder and holds everything users upload; «Вложения» is not a
# row at all — it is generated from the act, protocol and task attachment
# tables by `documents/references.py`. Nothing may be created at the root
# beside them.
CORPORATE_FOLDER_CODE = 'corporate'
CORPORATE_FOLDER_NAME = 'Корпоративные документы'


class DocumentFolder(models.Model):
    """One directory in the library, nested through a self-reference.

    `parent = NULL` means a top-level folder — a direct child of the browse
    root. Deleting a folder takes its subfolders and its documents with it;
    `documents/services.py` removes the stored files first, so a deletion does
    not leave orphans on disk.
    """

    name = models.CharField('Название', max_length=180)
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        related_name='children',
        verbose_name='Родительская папка',
        blank=True,
        null=True,
    )
    # A stable handle for the folders the project itself creates. Empty for
    # everything a user makes, so only system folders are ever addressed by
    # code and a future stage can find «Вложения» without a name comparison.
    code = models.CharField('Код', max_length=50, blank=True, default='')
    # System folders are the initial structure: they may receive content but
    # are not renamed or deleted from the page.
    is_system = models.BooleanField('Системная папка', default=False)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='created_document_folders',
        verbose_name='Создал',
        blank=True,
        null=True,
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлена', auto_now=True)

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'Папка документации'
        verbose_name_plural = 'Папки документации'
        constraints = [
            # Two constraints and not one: in SQL, NULL never equals NULL, so
            # a single (parent, name) pair would not stop two identically
            # named top-level folders.
            models.UniqueConstraint(
                fields=['parent', 'name'],
                condition=Q(parent__isnull=False),
                name='documents_folder_unique_name_in_parent',
            ),
            models.UniqueConstraint(
                fields=['name'],
                condition=Q(parent__isnull=True),
                name='documents_folder_unique_root_name',
            ),
            models.UniqueConstraint(
                fields=['code'],
                condition=~Q(code=''),
                name='documents_folder_unique_code',
            ),
        ]

    def __str__(self):
        return self.name

    def ancestors(self):
        """Every folder above this one, outermost first, this one excluded.

        Bounded by `MAX_FOLDER_DEPTH`, so a row that somehow acquired a cycle
        returns a truncated chain instead of hanging the page.
        """
        chain = []
        current = self.parent
        while current is not None and len(chain) < MAX_FOLDER_DEPTH:
            chain.append(current)
            current = current.parent
        chain.reverse()
        return chain

    def breadcrumbs(self):
        """The ancestors plus this folder — what the path line renders."""
        return [*self.ancestors(), self]

    @property
    def depth(self):
        """0 for a top-level folder."""
        return len(self.ancestors())

    @property
    def full_path(self):
        return ' / '.join([ROOT_FOLDER_LABEL, *(folder.name for folder in self.breadcrumbs())])


def _library_path(folder_id, filename):
    """`documents/library/<folder_id>/<uuid>.<ext>` — never the browser's name.

    Its own tree under MEDIA_ROOT, untouched by and untouching
    `acts/attachments/`, `protocols/attachments/` and the task attachments.
    The stored path contains no user-supplied text at all, so a crafted name
    cannot traverse out of the directory or collide with another upload — and
    because every version gets a fresh UUID, a new version can never overwrite
    the file of an older one. The real name lives in
    `DocumentVersion.original_name` and is used only for the download.
    """
    parts = (filename or '').rsplit('.', 1)
    extension = f'.{parts[1].lower()}' if len(parts) == 2 else ''
    folder = folder_id if folder_id is not None else 'unsorted'
    return f'documents/library/{folder}/{uuid4().hex}{extension}'


def document_upload_to(instance, filename):
    """Retained for the historical migrations that reference it by path.

    `Document` no longer stores a file — `DocumentVersion` does — but
    `documents/migrations/0001_initial.py` names this function, and a migration
    that cannot import its own field definition cannot be replayed on an empty
    database. Do not delete it.
    """
    return _library_path(getattr(instance, 'folder_id', None), filename)


def document_version_upload_to(instance, filename):
    """Where one version's file goes: under its document's folder."""
    folder_id = instance.document.folder_id if instance.document_id is not None else None
    return _library_path(folder_id, filename)


# `Document.versions` prefetched down to the current one, as
# `document.prefetched_current_versions`. A listing that supplies it asks one
# extra query for the whole page instead of one per row.
CURRENT_VERSION_ATTR = 'prefetched_current_versions'


class Document(models.Model):
    """One *logical* corporate document. The files are its versions.

    Deliberately no `file` here any more: a controlled document is an identity
    that outlives the particular PDF someone uploaded, and keeping the file on
    the document would mean a new revision either overwrote the old one or
    became a second, unrelated document. `name` and `folder` are the identity;
    `DocumentVersion` is the content.

    Only corporate documents work this way. Act, protocol and task attachments
    are projected read-only through `documents/references.py`; they have no
    versions and acquire none — see `can_modify_system_attachments()`.
    """

    folder = models.ForeignKey(
        DocumentFolder,
        on_delete=models.CASCADE,
        related_name='documents',
        verbose_name='Папка',
    )
    name = models.CharField('Название', max_length=255)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='uploaded_documents',
        verbose_name='Создал',
        blank=True,
        null=True,
    )
    uploaded_at = models.DateTimeField('Создан', auto_now_add=True)
    # Touched by `add_document_version()` as well, so a listing labelled with
    # it reflects the newest revision and not just a rename.
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'Документ'
        verbose_name_plural = 'Документы'
        indexes = [models.Index(fields=['folder', 'name'])]

    def __str__(self):
        return self.name

    @property
    def current_version(self):
        """The version users download, or None when the document has no files.

        None is a real state — a document whose only version was removed — and
        every caller renders it rather than raising. Reads the prefetch when a
        listing supplied one, so a page of documents costs one query for all
        of them instead of one each.
        """
        prefetched = getattr(self, CURRENT_VERSION_ATTR, None)
        if prefetched is not None:
            return prefetched[0] if prefetched else None
        return self.versions.filter(is_current=True).first()

    @staticmethod
    def current_version_prefetch():
        """`Prefetch` that fills `current_version` for a whole queryset at once.

        Defined here, beside the property that reads it, so a listing cannot
        drift from the attribute name the property looks for.
        """
        return models.Prefetch(
            'versions',
            queryset=DocumentVersion.objects.filter(is_current=True).select_related('uploaded_by'),
            to_attr=CURRENT_VERSION_ATTR,
        )

    @property
    def extension(self):
        version = self.current_version
        return version.extension if version is not None else ''


class DocumentVersion(models.Model):
    """One concrete uploaded file of a corporate document.

    Append-only: a new upload adds a row and clears `is_current` on the
    previous one. Nothing here edits or replaces a stored file, and every
    version gets its own UUID path, so each earlier revision stays downloadable
    exactly as it was uploaded. `number` counts from 1 per document and is
    allocated under a row lock in `add_document_version()`.

    `original_name`, `file_size` and `content_type` are copied at upload so a
    listing and a download work without touching storage, and so a file that
    later disappears from disk is still an identifiable row rather than a 500.

    **Where an approval workflow attaches.** A status, an approver, a decision
    date or an electronic signature belong on *this* row and not on `Document`:
    revisions are approved one at a time, and a document's approved revision is
    then whichever version carries the decision. None of that is implemented.
    """

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='versions',
        verbose_name='Документ',
    )
    file = models.FileField('Файл', upload_to=document_version_upload_to)
    number = models.PositiveIntegerField('Номер версии', default=1)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    file_size = models.PositiveBigIntegerField('Размер файла', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    comment = models.TextField('Комментарий к версии', blank=True)
    # Exactly one row per document carries this. The partial unique constraint
    # below is what makes «current» a fact rather than a convention.
    is_current = models.BooleanField('Текущая версия', default=False)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='uploaded_document_versions',
        verbose_name='Загрузил',
        blank=True,
        null=True,
    )
    uploaded_at = models.DateTimeField('Загружена', auto_now_add=True)

    class Meta:
        ordering = ['-number', '-pk']
        verbose_name = 'Версия документа'
        verbose_name_plural = 'Версии документов'
        constraints = [
            models.UniqueConstraint(
                fields=['document', 'number'],
                name='documents_version_unique_number_per_document',
            ),
            models.UniqueConstraint(
                fields=['document'],
                condition=Q(is_current=True),
                name='documents_version_single_current',
            ),
        ]

    def __str__(self):
        return f'{self.document.name} — v{self.number}'

    @property
    def label(self):
        return f'v{self.number}'

    @property
    def extension(self):
        parts = (self.original_name or '').rsplit('.', 1)
        return parts[1].lower() if len(parts) == 2 else ''


class DocumentHistoryEvent(models.Model):
    """What happened to a corporate document, in one small append-only table.

    Deliberately not an audit framework: four actions, a user, a timestamp and
    a sentence. It exists so «who replaced this instruction, and when» has an
    answer on the page instead of in a log file.

    `document` is nullable and the document's name is copied onto the row,
    because the one event a history most needs to keep is the one that deletes
    its subject — a cascade would erase exactly that record. A future approval
    stage adds members to `Action` and writes here through
    `documents/services.py`; it does not need another table.
    """

    class Action(models.TextChoices):
        DOCUMENT_CREATED = 'DOCUMENT_CREATED', 'Документ создан'
        VERSION_ADDED = 'VERSION_ADDED', 'Загружена версия'
        VERSION_RESTORED = 'VERSION_RESTORED', 'Версия восстановлена'
        DOCUMENT_DELETED = 'DOCUMENT_DELETED', 'Документ удалён'

    document = models.ForeignKey(
        Document,
        on_delete=models.SET_NULL,
        related_name='history',
        verbose_name='Документ',
        blank=True,
        null=True,
    )
    # A snapshot, so a deleted document's history still says what it was about.
    document_name = models.CharField('Название документа', max_length=255, blank=True)
    version = models.ForeignKey(
        DocumentVersion,
        on_delete=models.SET_NULL,
        related_name='history',
        verbose_name='Версия',
        blank=True,
        null=True,
    )
    version_number = models.PositiveIntegerField('Номер версии', blank=True, null=True)
    action = models.CharField('Событие', max_length=32, choices=Action.choices)
    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='document_history_events',
        verbose_name='Пользователь',
        blank=True,
        null=True,
    )
    description = models.TextField('Описание', blank=True)
    # A plain field with a default rather than `auto_now_add`, so the data
    # migration can backfill an existing document's real creation time instead
    # of stamping every historical event with the moment of deployment.
    created_at = models.DateTimeField('Когда', default=timezone.now, db_index=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Событие документа'
        verbose_name_plural = 'История документов'

    def __str__(self):
        return f'{self.document_name}: {self.get_action_display()}'
