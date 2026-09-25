"""Fill `DocumentVersion.text_content` for «поиск по тексту».

Versions uploaded before text extraction existed have no text; this reads
their files once. `--all` re-reads every version, for instance after the
extractor learned a new format. Read-only towards the files.
"""

from django.core.management.base import BaseCommand

from documents.models import DocumentVersion
from documents.text_extraction import extract_version_text


class Command(BaseCommand):
    help = 'Извлекает текст из файлов документов для полнотекстового поиска.'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Перечитать и версии, у которых текст уже есть.')

    def handle(self, *args, **options):
        versions = DocumentVersion.objects.order_by('pk')
        if not options['all']:
            versions = versions.filter(text_content='')
        updated = 0
        for version in versions.iterator():
            text = extract_version_text(version)
            if text != version.text_content:
                DocumentVersion.objects.filter(pk=version.pk).update(text_content=text)
                updated += 1
        self.stdout.write(f'Обновлён текст версий: {updated}.')
