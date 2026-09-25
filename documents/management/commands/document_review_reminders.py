"""Raise «Пересмотреть документ» tasks for review dates that are near.

Run once a day (Task Scheduler / cron). Idempotent: a document whose review
task already exists for its current review date is skipped, so running it
twice — or every hour — changes nothing.
"""

from django.core.management.base import BaseCommand

from documents.services import REVIEW_LEAD_DAYS, create_review_tasks


class Command(BaseCommand):
    help = 'Создаёт задачи «Пересмотреть документ» за N дней до даты пересмотра.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=REVIEW_LEAD_DAYS)

    def handle(self, *args, **options):
        created = create_review_tasks(lead_days=max(0, options['days']))
        self.stdout.write(f'Создано задач на пересмотр: {len(created)}.')
