"""Group the writable library under «Корпоративные документы».

The root of the documentation tree now holds exactly two branches: this
folder, which is everything users upload, and «Вложения», which is generated
from the act, protocol and task attachment tables and has no rows at all.

Idempotent in both directions of a redeploy: `ensure_default_folders()`
matches on `code`, and the reparenting step only looks at folders still left
at the root, so a second run finds none.
"""

from django.db import migrations

from documents.services import ensure_default_folders


def group_under_corporate_root(apps, schema_editor):
    DocumentFolder = apps.get_model('documents', 'DocumentFolder')
    ensure_default_folders(DocumentFolder)
    corporate = DocumentFolder.objects.get(code='corporate')
    # Everything that was a top-level folder before this migration — the five
    # shipped folders and anything an administrator had already created —
    # moves inside. `updated_at` is `auto_now`, so a plain update() is used to
    # leave the timestamps alone: nothing about the folders themselves changed.
    DocumentFolder.objects.filter(parent__isnull=True).exclude(pk=corporate.pk).update(
        parent=corporate
    )


def ungroup_from_corporate_root(apps, schema_editor):
    """Put the children back at the root and drop the wrapper if it is empty."""
    DocumentFolder = apps.get_model('documents', 'DocumentFolder')
    corporate = DocumentFolder.objects.filter(code='corporate').first()
    if corporate is None:
        return
    DocumentFolder.objects.filter(parent=corporate).update(parent=None)
    if not corporate.documents.exists():
        corporate.delete()


class Migration(migrations.Migration):

    dependencies = [('documents', '0002_default_folders')]

    operations = [
        migrations.RunPython(group_under_corporate_root, ungroup_from_corporate_root),
    ]
