"""The initial library structure, created once and never duplicated.

`ensure_default_folders()` matches on `code`, so re-running this migration on
a database that already has the folders finds them instead of creating a
second set — and it keeps them even after somebody renames one in Admin. The
same function is importable for a deploy check, which is why the list of
folders is not repeated here.
"""

from django.db import migrations

from documents.services import ensure_default_folders


def create_default_folders(apps, schema_editor):
    ensure_default_folders(apps.get_model('documents', 'DocumentFolder'))


def remove_default_folders(apps, schema_editor):
    """Reverse only the untouched, still-empty system folders.

    A folder that has acquired documents or subfolders is somebody's content
    now; unapplying a migration must not take it away.
    """
    DocumentFolder = apps.get_model('documents', 'DocumentFolder')
    from documents.services import DEFAULT_FOLDERS

    for code, _name in DEFAULT_FOLDERS:
        folder = DocumentFolder.objects.filter(code=code, is_system=True).first()
        if folder is None:
            continue
        if folder.documents.exists() or folder.children.exists():
            continue
        folder.delete()


class Migration(migrations.Migration):

    dependencies = [('documents', '0001_initial')]

    operations = [
        migrations.RunPython(create_default_folders, remove_default_folders),
    ]
