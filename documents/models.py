"""The documentation library: a folder tree and the files inside it.

Two tables, both named for what they are and not for who uploaded them —
`DocumentFolder` and `Document`, never `UserFile` — because the same tree is
meant to grow a second branch later:

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


# How deep the tree may go. Not a storage limit — a guard so a breadcrumb
# stays readable and a recursive walk stays bounded.
MAX_FOLDER_DEPTH = 10

# What the breadcrumb shows before the first real folder.
ROOT_FOLDER_LABEL = 'Документация'


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


def document_upload_to(instance, filename):
    """`documents/library/<folder_id>/<uuid>.<ext>` — never the browser's name.

    Its own tree under MEDIA_ROOT, untouched by and untouching
    `acts/attachments/`, `protocols/attachments/` and the task attachments.
    The stored path contains no user-supplied text at all, so a crafted name
    cannot traverse out of the directory or collide with another upload; the
    real name lives in `original_name` and is used only for the download.
    """
    parts = (filename or '').rsplit('.', 1)
    extension = f'.{parts[1].lower()}' if len(parts) == 2 else ''
    folder_id = instance.folder_id or 'unsorted'
    return f'documents/library/{folder_id}/{uuid4().hex}{extension}'


class Document(models.Model):
    """One file in the library.

    `name` is what the browser shows and may be edited independently of the
    file; `original_name`, `file_size` and `content_type` are copied at upload
    so a listing and a download work without touching storage, and so a file
    that later disappears from disk is still an identifiable row rather than a
    500.
    """

    folder = models.ForeignKey(
        DocumentFolder,
        on_delete=models.CASCADE,
        related_name='documents',
        verbose_name='Папка',
    )
    file = models.FileField('Файл', upload_to=document_upload_to)
    name = models.CharField('Название', max_length=255)
    original_name = models.CharField('Исходное имя файла', max_length=255)
    file_size = models.PositiveBigIntegerField('Размер файла', default=0)
    content_type = models.CharField('Тип содержимого', max_length=120, blank=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='uploaded_documents',
        verbose_name='Загрузил',
        blank=True,
        null=True,
    )
    uploaded_at = models.DateTimeField('Загружен', auto_now_add=True)
    updated_at = models.DateTimeField('Обновлен', auto_now=True)

    class Meta:
        ordering = ['name', 'pk']
        verbose_name = 'Документ'
        verbose_name_plural = 'Документы'
        indexes = [models.Index(fields=['folder', 'name'])]

    def __str__(self):
        return self.name

    @property
    def extension(self):
        parts = (self.original_name or '').rsplit('.', 1)
        return parts[1].lower() if len(parts) == 2 else ''
