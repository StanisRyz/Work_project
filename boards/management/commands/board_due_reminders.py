"""Remind the people of a board card that its срок is near or has passed.

Run once a day (Task Scheduler / cron), beside `document_review_reminders`.
Idempotent: a reminder is keyed on the task and its срок, so running it twice
— or every hour — creates nothing more, and a срок moved asks again.
"""

from django.core.management.base import BaseCommand

from boards.services import send_due_reminders


class Command(BaseCommand):
    help = 'Напоминает о сроке карточек досок: «завтра срок» исполнителям, «просрочена» — всем на карточке.'

    def handle(self, *args, **options):
        created = send_due_reminders()
        self.stdout.write(
            f'Напоминаний «срок подходит»: {created["due_soon"]}, «просрочено»: {created["overdue"]}.'
        )
