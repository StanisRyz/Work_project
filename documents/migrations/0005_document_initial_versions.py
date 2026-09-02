"""Turn every existing corporate document into its own version 1.

The file itself is **not touched**: only the stored path is copied from
`Document.file` onto the new `DocumentVersion.file`, so nothing is moved,
re-uploaded or duplicated in MEDIA_ROOT and every existing download keeps
resolving to the same bytes. `0006` drops the now-unused columns afterwards.

Two history rows are backfilled per document — created, and version 1 loaded —
with the document's real timestamps rather than the moment of deployment, so
the history page of an existing document is truthful rather than empty.

System attachments are untouched by design: they have no `Document` row, no
version and no history, and this migration never reads their tables.

Idempotent: a document that already has versions is skipped, so a re-run adds
nothing.
"""

from django.db import migrations


def create_initial_versions(apps, schema_editor):
    Document = apps.get_model('documents', 'Document')
    DocumentVersion = apps.get_model('documents', 'DocumentVersion')
    DocumentHistoryEvent = apps.get_model('documents', 'DocumentHistoryEvent')

    for document in Document.objects.exclude(versions__isnull=False).iterator():
        version = DocumentVersion.objects.create(
            document=document,
            # The stored path, assigned as a plain string: this points the new
            # row at the file that is already on disk.
            file=document.file.name if document.file else '',
            number=1,
            original_name=document.original_name or document.name,
            file_size=document.file_size or 0,
            content_type=document.content_type or '',
            is_current=True,
            uploaded_by=document.uploaded_by,
        )
        # `uploaded_at` is auto_now_add, so the real upload time has to be
        # written after the insert rather than passed to it.
        DocumentVersion.objects.filter(pk=version.pk).update(uploaded_at=document.uploaded_at)

        DocumentHistoryEvent.objects.bulk_create([
            DocumentHistoryEvent(
                document=document,
                document_name=document.name,
                action='DOCUMENT_CREATED',
                user=document.uploaded_by,
                description='Документ создан.',
                created_at=document.uploaded_at,
            ),
            DocumentHistoryEvent(
                document=document,
                document_name=document.name,
                version=version,
                version_number=1,
                action='VERSION_ADDED',
                user=document.uploaded_by,
                description='Загружена версия v1.',
                created_at=document.uploaded_at,
            ),
        ])


def drop_versions(apps, schema_editor):
    """Reverse by copying version 1 back onto the document and clearing both.

    The file stays where it is here too — only the pointer moves back.
    """
    Document = apps.get_model('documents', 'Document')
    DocumentVersion = apps.get_model('documents', 'DocumentVersion')
    DocumentHistoryEvent = apps.get_model('documents', 'DocumentHistoryEvent')

    for document in Document.objects.iterator():
        version = document.versions.order_by('number').first()
        if version is None:
            continue
        document.file = version.file.name
        document.original_name = version.original_name
        document.file_size = version.file_size
        document.content_type = version.content_type
        document.save(update_fields=['file', 'original_name', 'file_size', 'content_type'])
    DocumentHistoryEvent.objects.all().delete()
    DocumentVersion.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [('documents', '0004_versioning')]

    operations = [migrations.RunPython(create_initial_versions, drop_versions)]
