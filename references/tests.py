from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from references.models import DEVIATION_REASONS, ActStatus, DefectType, DeviationReason, Operation, Priority, TaskStatus


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
        self.assertIn('3 task statuses', output)

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


class DeviationReasonTests(TestCase):
    """«Причины отклонений»: seeded by `references.0005`, kept by `seed_references`."""

    def test_the_migration_seeded_the_eight_reasons(self):
        self.assertEqual(
            list(DeviationReason.objects.order_by('display_order').values_list('code', flat=True)),
            [code for code, _name, _order in DEVIATION_REASONS],
        )
        self.assertTrue(all(DeviationReason.objects.values_list('is_active', flat=True)))

    def test_the_migration_seed_is_idempotent(self):
        from importlib import import_module

        from django.apps import apps

        migration = import_module('references.migrations.0005_deviation_reasons')
        migration.seed_reasons(apps, None)
        migration.seed_reasons(apps, None)
        self.assertEqual(DeviationReason.objects.count(), len(DEVIATION_REASONS))

    def test_seed_references_keeps_what_admin_changed(self):
        DeviationReason.objects.filter(code='PAYMENT').update(is_active=False, name='Оплата')
        DeviationReason.objects.filter(code='OTHER').delete()
        out = StringIO()
        call_command('seed_references', stdout=out)
        call_command('seed_references', stdout=StringIO())
        self.assertEqual(DeviationReason.objects.count(), len(DEVIATION_REASONS))
        payment = DeviationReason.objects.get(code='PAYMENT')
        self.assertEqual((payment.is_active, payment.name), (False, 'Оплата'))
        self.assertIn(f'{len(DEVIATION_REASONS)} deviation reasons', out.getvalue())
