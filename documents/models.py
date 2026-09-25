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

# How long a document stays in «Корзина» before `purge_document_trash` removes
# it for good.
TRASH_RETENTION_DAYS = 30

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
    # Who may see this folder and everything under it, as `UserProfile.Role`
    # codes. Empty — the default — is «every employee»: the library is open for
    # reading, and a folder is closed only when somebody decides so. A
    # restriction inherits downwards (`documents.permissions.can_view_folder()`
    # walks the ancestors), and document managers always see everything, so a
    # folder can never be closed to the people who keep it.
    allowed_roles = models.JSONField('Доступ только для ролей', default=list, blank=True)
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


class LiveDocumentManager(models.Manager):
    """Documents that are not in «Корзина». The default: nothing lists, finds
    or counts a trashed document unless it asks `Document.all_objects`."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


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

    # ------------------------------------------------------------------
    # The document card — what makes a file a *controlled* document
    # (ISO 9001 7.5): what it is called officially, whether it is in force,
    # since when, who owns it and when it must be looked at again. All
    # optional, because documents uploaded before the card existed have none
    # of it and nothing may be invented for them.
    # ------------------------------------------------------------------

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', 'Проект'
        ACTIVE = 'ACTIVE', 'Действующий'
        # Withdrawn, never deleted: a controlled document that stops applying
        # is marked so and kept, with the reason, because acts and protocols
        # decided under it still refer to it.
        CANCELLED = 'CANCELLED', 'Отменён'

    designation = models.CharField('Обозначение', max_length=80, blank=True)
    status = models.CharField('Статус', max_length=16, choices=Status.choices, default=Status.ACTIVE)
    effective_date = models.DateField('Дата введения', blank=True, null=True)
    # When the document must be looked at again. `document_review_reminders`
    # turns an approaching date into a «Пересмотреть документ» task for
    # `responsible`.
    review_date = models.DateField('Дата пересмотра', blank=True, null=True)
    owner_department = models.ForeignKey(
        'accounts.Department',
        on_delete=models.SET_NULL,
        related_name='owned_documents',
        verbose_name='Подразделение-владелец',
        blank=True,
        null=True,
    )
    responsible = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='responsible_documents',
        verbose_name='Ответственный',
        blank=True,
        null=True,
    )
    cancelled_at = models.DateTimeField('Отменён', blank=True, null=True)
    cancelled_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='cancelled_documents',
        verbose_name='Отменил',
        blank=True,
        null=True,
    )
    cancellation_reason = models.TextField('Причина отмены', blank=True)
    # «Корзина»: set by `trash_document()`, cleared by `restore_document()`.
    # A trashed document is invisible everywhere but the trash page, and
    # `purge_document_trash` removes it for good after `TRASH_RETENTION_DAYS`.
    deleted_at = models.DateTimeField('В корзине с', blank=True, null=True, db_index=True)
    deleted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='trashed_documents',
        verbose_name='Отправил в корзину',
        blank=True,
        null=True,
    )

    # The first manager is the default one, and it hides «Корзина». Relations
    # from a version or a task use the base manager, so they still reach a
    # trashed document.
    objects = LiveDocumentManager()
    all_objects = models.Manager()

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'Документ'
        verbose_name_plural = 'Документы'
        indexes = [models.Index(fields=['folder', 'name'])]
        base_manager_name = 'all_objects'

    def __str__(self):
        return self.title

    @property
    def title(self):
        """«ДП-СМК 07.04 Управление несоответствующей продукцией» — the
        designation first when there is one, as the plant writes it."""
        if self.designation and not self.name.startswith(self.designation):
            return f'{self.designation} {self.name}'
        return self.name

    @property
    def is_trashed(self):
        return self.deleted_at is not None

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
    # «изм. 2» — how the document itself numbers its amendments. Free text and
    # optional: `number` is the library's own counter and is never shown as the
    # official revision.
    revision_label = models.CharField('Изменение', max_length=40, blank=True)
    # The text of the file, extracted once at upload by
    # `documents/text_extraction.py`, for «поиск по тексту». Empty for a type
    # nothing can be read from (a scan, an image) — the search then simply
    # does not find it by content.
    text_content = models.TextField('Текст документа', blank=True)

    class Approval(models.TextChoices):
        # Uploaded without a round: current at once, as every version was
        # before approval existed.
        NONE = '', 'Без согласования'
        PENDING = 'PENDING', 'На согласовании'
        APPROVED = 'APPROVED', 'Согласована'
        RETURNED = 'RETURNED', 'Возвращена'

    # Where the version stands in its own approval round. A `PENDING` version
    # is stored but not current; the last approval makes it current in the
    # same transaction (`documents/services.approve_version()`), a return
    # leaves it a readable, non-current row with `approval_comment`.
    approval_status = models.CharField(
        'Согласование', max_length=16, choices=Approval.choices, blank=True, default='',
    )
    approval_comment = models.TextField('Причина возврата', blank=True)
    # Who must acknowledge this version once it is in force, as the uploader
    # chose them: role codes and department ids. Kept on the version because a
    # version on approval is not current yet — the acknowledgement tasks are
    # issued the moment it becomes current, not at upload.
    ack_roles = models.JSONField('Ознакомить роли', default=list, blank=True)
    ack_department_ids = models.JSONField('Ознакомить подразделения', default=list, blank=True)

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
    def full_label(self):
        """«v3 · изм. 2» when the document numbers its amendments, else «v3»."""
        return f'{self.label} · {self.revision_label}' if self.revision_label else self.label

    @property
    def extension(self):
        parts = (self.original_name or '').rsplit('.', 1)
        return parts[1].lower() if len(parts) == 2 else ''


class DocumentFavorite(models.Model):
    """One user's shortcut to one corporate document. Private to that user.

    A join row and nothing more: no name, no order, no sharing. Two people
    starring the same document get two independent rows, and neither can see
    or affect the other's — the uniqueness is per `(user, document)`, so the
    table cannot express «everybody's favourite» even by accident.

    Corporate documents only. A system attachment has no `Document` row to
    point at, which is exactly why it cannot be favourited: the shortcut would
    have to name a file this module does not own.
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='document_favorites',
        verbose_name='Пользователь',
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name='favorites',
        verbose_name='Документ',
    )
    created_at = models.DateTimeField('Добавлен', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Избранный документ'
        verbose_name_plural = 'Избранные документы'
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'document'],
                name='documents_favorite_unique_per_user',
            ),
        ]

    def __str__(self):
        return f'{self.user}: {self.document}'

    @staticmethod
    def ids_for(user, documents):
        """Which of `documents` this user has starred, as a set of ids.

        One query for a whole listing instead of one per card, and an empty
        set for an anonymous user — a card then simply renders without the
        star, which is what an unauthenticated viewer should see.
        """
        if not getattr(user, 'is_authenticated', False):
            return frozenset()
        ids = [document.pk for document in documents]
        if not ids:
            return frozenset()
        return frozenset(
            DocumentFavorite.objects.filter(user=user, document_id__in=ids).values_list(
                'document_id', flat=True
            )
        )


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
        CARD_UPDATED = 'CARD_UPDATED', 'Изменена карточка'
        MOVED = 'MOVED', 'Перенесён'
        TRASHED = 'TRASHED', 'Отправлен в корзину'
        RESTORED = 'RESTORED', 'Восстановлен из корзины'
        STATUS_CHANGED = 'STATUS_CHANGED', 'Изменён статус'
        VERSION_SUBMITTED = 'VERSION_SUBMITTED', 'Версия отправлена на согласование'
        VERSION_APPROVED = 'VERSION_APPROVED', 'Версия согласована'
        VERSION_RETURNED = 'VERSION_RETURNED', 'Версия возвращена'
        ACK_REQUESTED = 'ACK_REQUESTED', 'Разослан на ознакомление'
        LINKED = 'LINKED', 'Связан с документом системы'
        UNLINKED = 'UNLINKED', 'Связь удалена'

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


class DocumentVersionApproval(models.Model):
    """One approver's decision on one version — the protocol approval's shape.

    A row per `(version, user)`, created when the version is uploaded with a
    round. `task` is the approver's «Согласовать документ» queue entry, closed
    by the decision itself; nothing here is completed from «Задачи».
    """

    class Status(models.TextChoices):
        PENDING = 'PENDING', 'Ожидает'
        APPROVED = 'APPROVED', 'Согласовано'
        RETURNED = 'RETURNED', 'Возвращено'
        CANCELLED = 'CANCELLED', 'Отменено'

    version = models.ForeignKey(
        DocumentVersion, on_delete=models.CASCADE, related_name='approvals', verbose_name='Версия',
    )
    user = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name='document_approvals', verbose_name='Согласующий',
    )
    status = models.CharField('Решение', max_length=16, choices=Status.choices, default=Status.PENDING)
    comment = models.TextField('Комментарий', blank=True)
    decided_at = models.DateTimeField('Решение принято', blank=True, null=True)
    task = models.OneToOneField(
        'tasks.Task', on_delete=models.SET_NULL, related_name='document_approval',
        blank=True, null=True, verbose_name='Задача согласования',
    )
    created_at = models.DateTimeField('Создано', auto_now_add=True)

    class Meta:
        ordering = ['pk']
        verbose_name = 'Согласование версии'
        verbose_name_plural = 'Согласования версий'
        constraints = [
            models.UniqueConstraint(fields=['version', 'user'], name='documents_approval_unique_per_user'),
        ]

    def __str__(self):
        return f'{self.version}: {self.user}'


class DocumentLink(models.Model):
    """«Где используется»: a document cited by an act or a protocol.

    Exactly one of `act` and `protocol` is set — the check constraint says so —
    and a pair is linked at most once. The link belongs to Documentation; the
    act and the protocol only show it.
    """

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name='links', verbose_name='Документ',
    )
    act = models.ForeignKey(
        'acts.Act', on_delete=models.CASCADE, related_name='document_links',
        blank=True, null=True, verbose_name='Акт',
    )
    protocol = models.ForeignKey(
        'protocols.Protocol', on_delete=models.CASCADE, related_name='document_links',
        blank=True, null=True, verbose_name='Протокол',
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, related_name='+', blank=True, null=True, verbose_name='Добавил',
    )
    created_at = models.DateTimeField('Добавлена', auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        verbose_name = 'Ссылка на документ'
        verbose_name_plural = 'Ссылки на документы'
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(act__isnull=False, protocol__isnull=True)
                    | Q(act__isnull=True, protocol__isnull=False)
                ),
                name='documents_link_exactly_one_target',
            ),
            models.UniqueConstraint(
                fields=['document', 'act'], condition=Q(act__isnull=False),
                name='documents_link_unique_act',
            ),
            models.UniqueConstraint(
                fields=['document', 'protocol'], condition=Q(protocol__isnull=False),
                name='documents_link_unique_protocol',
            ),
        ]


class DocumentSubscription(models.Model):
    """«Подписаться»: tell me when a new version comes into force.

    On one document or on a whole folder (its documents at any depth). Private
    to the user, like a favourite; exactly one target per row.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name='document_subscriptions', verbose_name='Пользователь',
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name='subscriptions',
        blank=True, null=True, verbose_name='Документ',
    )
    folder = models.ForeignKey(
        DocumentFolder, on_delete=models.CASCADE, related_name='subscriptions',
        blank=True, null=True, verbose_name='Папка',
    )
    created_at = models.DateTimeField('Создана', auto_now_add=True)

    class Meta:
        verbose_name = 'Подписка на документы'
        verbose_name_plural = 'Подписки на документы'
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(document__isnull=False, folder__isnull=True)
                    | Q(document__isnull=True, folder__isnull=False)
                ),
                name='documents_subscription_exactly_one_target',
            ),
            models.UniqueConstraint(
                fields=['user', 'document'], condition=Q(document__isnull=False),
                name='documents_subscription_unique_document',
            ),
            models.UniqueConstraint(
                fields=['user', 'folder'], condition=Q(folder__isnull=False),
                name='documents_subscription_unique_folder',
            ),
        ]

