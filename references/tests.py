from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from references.models import ActStatus, DefectType, Operation, Priority, TaskStatus


class SeedReferencesCommandTests(TestCase):
    def test_the_summary_message_reports_actual_counts(self):
        out = StringIO()
        call_command('seed_references', stdout=out)
        output = out.getvalue()

        self.assertIn(f'{Operation.objects.count()} operations', output)
        self.assertIn(f'{DefectType.objects.count()} defect types', output)
        self.assertIn(f'{ActStatus.objects.count()} act statuses', output)
        self.assertIn(f'{TaskStatus.objects.count()} task statuses', output)
        self.assertIn(f'{Priority.objects.count()} priorities', output)
        # The historical bug: the printed task status count did not match the
        # two rows the command actually creates.
        self.assertIn('2 task statuses', output)

    def test_is_idempotent_and_safe_to_run_twice(self):
        call_command('seed_references', stdout=StringIO())
        first_counts = (
            Operation.objects.count(),
            DefectType.objects.count(),
            ActStatus.objects.count(),
            TaskStatus.objects.count(),
            Priority.objects.count(),
        )

        call_command('seed_references', stdout=StringIO())
        second_counts = (
            Operation.objects.count(),
            DefectType.objects.count(),
            ActStatus.objects.count(),
            TaskStatus.objects.count(),
            Priority.objects.count(),
        )

        self.assertEqual(first_counts, second_counts)
