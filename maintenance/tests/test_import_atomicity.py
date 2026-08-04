import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings

from acts.models import Act, ActNumberSequence
from maintenance import database_transfer as dt

from .test_bundle import BundleFixtureMixin


class ImportTargetMixin(BundleFixtureMixin):
    """Exports a bundle, then empties every transferable table.

    The vendor is patched to PostgreSQL because that is the only backend the
    import allows; on SQLite the sequence reset is a documented no-op and the
    rest of the code path is identical, so the transaction behaviour under test
    is the real one.
    """

    def setUpImportTarget(self, name='source', missing_media=False):
        self.setUpBundleData()
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with override_settings(MEDIA_ROOT=self.media_root):
                self.attachment = self.add_attachment()
                if missing_media:
                    (self.media_root / self.attachment.file.name).unlink()
                dt.export_bundle(
                    self.bundle_path(name), allow_missing_media=missing_media
                )
        self.bundle = self.bundle_path(name)

        self.target_media = Path(tempfile.mkdtemp(prefix='target-media-'))
        self.addCleanup(shutil.rmtree, self.target_media, ignore_errors=True)
        for label in reversed(dt.TRANSFERABLE_MODELS):
            dt.apps.get_model(label)._default_manager.all().delete()

    def import_bundle(self, **kwargs):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(
                MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False
            ):
                return dt.import_bundle(self.bundle, **kwargs)

    def call_import_command(self, *arguments, stdout=None):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(
                MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False
            ):
                return call_command(
                    'import_migration_bundle', '--input', str(self.bundle), *arguments,
                    stdout=stdout or StringIO(),
                )


class ImportAtomicityTests(ImportTargetMixin, TestCase):
    """Fixture load, sequence reset and ActNumberSequence sync share one
    transaction, so a failure in any of them leaves the target untouched."""

    def setUp(self):
        self.setUpImportTarget()

    def test_a_successful_import_loads_data_media_and_counters(self):
        result = self.import_bundle()

        self.assertEqual(result['status'], 'ok')
        self.assertGreater(result['loaded'], 0)
        self.assertEqual(len(result['media']['copied']), 1)
        self.assertTrue(result['complete_bundle'])
        self.assertEqual(Act.objects.count(), 1)
        self.assertTrue((self.target_media / 'acts/attachments/1/sample.txt').is_file())

    def test_a_failing_act_number_sync_rolls_the_whole_import_back(self):
        with mock.patch.object(
            dt, 'sync_act_number_sequences', side_effect=dt.TransferError('сбой синхронизации')
        ):
            with self.assertRaisesMessage(dt.TransferError, 'сбой синхронизации'):
                self.import_bundle()

        self.assertEqual(Act.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(ActNumberSequence.objects.count(), 0)
        self.assertFalse([entry for entry in self.target_media.rglob('*') if entry.is_file()])

    def test_a_failing_sequence_reset_rolls_the_whole_import_back(self):
        with mock.patch.object(
            dt, 'reset_database_sequences', side_effect=dt.TransferError('сбой последовательностей')
        ):
            with self.assertRaises(dt.TransferError):
                self.import_bundle()

        self.assertEqual(Act.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)
        self.assertFalse([entry for entry in self.target_media.rglob('*') if entry.is_file()])

    def test_act_number_sequences_are_synchronised_before_commit(self):
        observed = {}
        original = dt.sync_act_number_sequences

        def spy():
            observed['in_atomic_block'] = connection.in_atomic_block
            return original()

        with mock.patch.object(dt, 'sync_act_number_sequences', side_effect=spy):
            self.import_bundle()

        self.assertTrue(observed['in_atomic_block'])

    def test_a_media_activation_failure_yields_a_partial_result(self):
        with mock.patch.object(
            dt,
            '_activate_media',
            return_value={'ok': False, 'copied': [], 'error': 'нет доступа к каталогу'},
        ):
            result = self.import_bundle()

        self.assertEqual(result['status'], 'partial')
        self.assertIn('нет доступа', result['media']['error'])
        self.assertTrue(result['recovery'])
        self.assertIn('verify_migration_bundle', ' '.join(result['recovery']))
        # The database part did commit — that is exactly what makes it partial.
        self.assertEqual(Act.objects.count(), 1)

    def test_the_command_refuses_to_report_a_partial_import_as_success(self):
        with mock.patch.object(
            dt,
            '_activate_media',
            return_value={'ok': False, 'copied': [], 'error': 'нет доступа к каталогу'},
        ):
            with self.assertRaises(CommandError) as ctx:
                self.call_import_command()

        self.assertIn('частично', str(ctx.exception))


class IncompleteBundleImportCommandTests(ImportTargetMixin, TestCase):
    def setUp(self):
        self.setUpImportTarget(name='incomplete', missing_media=True)

    def test_the_command_refuses_an_incomplete_bundle_without_the_flag(self):
        with self.assertRaises(CommandError) as ctx:
            self.call_import_command()

        self.assertIn('--accept-missing-media', str(ctx.exception))
        self.assertEqual(Act.objects.count(), 0)

    def test_accept_missing_media_requires_the_exact_confirmation(self):
        with self.assertRaisesMessage(CommandError, 'Подтверждение'):
            self.call_import_command('--accept-missing-media', '--confirmation', 'нет')

        self.assertEqual(Act.objects.count(), 0)

    def test_a_confirmed_incomplete_import_is_never_reported_as_complete(self):
        buffer = StringIO()

        self.call_import_command(
            '--accept-missing-media',
            '--confirmation',
            'ПРИНЯТЬ НЕПОЛНЫЙ ПЕРЕНОС',
            stdout=buffer,
        )

        output = buffer.getvalue()
        self.assertIn('НЕПОЛНЫЙ', output)
        self.assertIn(str(self.attachment.pk), output)
        self.assertEqual(Act.objects.count(), 1)
