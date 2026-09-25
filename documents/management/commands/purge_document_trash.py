"""Remove documents that have been in «Корзина» longer than the retention period.

Run once a day. A document tasks were issued on (acknowledgement, approval,
review) is a record and stays in the trash; the command reports how many it
kept for that reason.
"""

from django.core.management.base import BaseCommand

from documents.models import TRASH_RETENTION_DAYS
from documents.services import purge_expired_trash


class Command(BaseCommand):
    help = f'Удаляет документы, пролежавшие в корзине дольше {TRASH_RETENTION_DAYS} дней.'

    def handle(self, *args, **options):
        purged, kept = purge_expired_trash()
        self.stdout.write(f'Удалено навсегда: {purged}. Оставлено как записи: {kept}.')
