"""Drop the file columns `Document` no longer owns.

Runs only after `0005_document_initial_versions` has copied every one of them
into a `DocumentVersion`. Nothing in MEDIA_ROOT is affected: these are
pointers, and the version rows now hold them.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [('documents', '0005_document_initial_versions')]

    operations = [
        migrations.RemoveField(model_name='document', name='file'),
        migrations.RemoveField(model_name='document', name='original_name'),
        migrations.RemoveField(model_name='document', name='file_size'),
        migrations.RemoveField(model_name='document', name='content_type'),
    ]
