"""Read-only audit of acts that still carry no `ActDefect` row.

`ActDefect` is the only source of defect data, so an act without one has
nothing left to describe its defect once the legacy summary columns are gone.
This command reports those acts before the schema cleanup runs; it prints
identifiers and counts only, never customer, defect or attachment text.
"""

from django.core.management.base import BaseCommand

from acts.models import Act


LEGACY_PK_LIMIT = 50


class Command(BaseCommand):
    help = 'Report acts that have no related ActDefect. Reads only, changes nothing.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--limit',
            type=int,
            default=LEGACY_PK_LIMIT,
            help=f'How many legacy-only act primary keys to print (default {LEGACY_PK_LIMIT}).',
        )

    def handle(self, *args, **options):
        limit = max(options['limit'], 0)
        total = Act.objects.count()
        legacy_only = Act.objects.filter(defects__isnull=True)
        legacy_count = legacy_only.count()
        with_defects = total - legacy_count

        self.stdout.write(f'Всего актов: {total}')
        self.stdout.write(f'Актов с дефектами: {with_defects}')
        self.stdout.write(f'Актов без дефектов: {legacy_count}')

        if not legacy_count:
            self.stdout.write(
                self.style.SUCCESS(
                    'Актов без дефектов нет — удаление устаревших полей Act безопасно.'
                )
            )
            return

        pks = list(legacy_only.order_by('pk').values_list('pk', flat=True)[:limit])
        self.stdout.write(f'PK актов без дефектов (до {limit}): {pks}')
        if legacy_count > len(pks):
            self.stdout.write(f'…и ещё {legacy_count - len(pks)}.')
        self.stdout.write(
            self.style.ERROR(
                'Удаление устаревших полей Act невозможно: сначала обработайте эти акты вручную.'
            )
        )
