import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings

from acts.models import Act
from maintenance import database_transfer as dt
from maintenance import preflight

from .test_bundle import BundleFixtureMixin


def check_by_name(report, name):
    for check in report['checks']:
        if check['name'] == name:
            return check
    raise AssertionError(f'В отчёте нет проверки {name}: {[c["name"] for c in report["checks"]]}')


class SourcePreflightTests(BundleFixtureMixin, TestCase):
    """The source check is read-only and must catch every export blocker."""

    def setUp(self):
        self.setUpBundleData()
        # Tests run against an in-memory SQLite database, so the *file* checks
        # are pointed at a stand-in copy on disk.
        self.database_copy = self.output_parent / 'db-copy.sqlite3'
        self.database_copy.write_bytes(b'SQLite format 3\x00')

    def _run(self, **kwargs):
        kwargs.setdefault('source_media_root', self.media_root)
        kwargs.setdefault('allow_default_database', True)
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with mock.patch.object(
                preflight, 'get_sqlite_database_path', return_value=self.database_copy
            ):
                with override_settings(MEDIA_ROOT=self.media_root):
                    return preflight.run_source_preflight(**kwargs)

    def test_a_healthy_source_passes_every_check(self):
        self.add_attachment()

        report = self._run()

        self.assertTrue(report['ok'], report['failures'])
        self.assertEqual(check_by_name(report, 'backend')['status'], preflight.OK)
        self.assertEqual(check_by_name(report, 'integrity_check')['status'], preflight.OK)
        self.assertEqual(check_by_name(report, 'attachment_files')['status'], preflight.OK)
        self.assertEqual(report['summary']['models'], len(dt.TRANSFERABLE_MODELS))
        self.assertGreater(report['summary']['total_rows'], 0)
        self.assertEqual(report['summary']['attachments']['files_present'], 1)

    def test_a_non_sqlite_backend_is_refused(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with self.assertRaisesMessage(dt.TransferError, 'только на SQLite'):
                preflight.run_source_preflight(allow_default_database=True)

    def test_a_corrupted_sqlite_file_fails_the_integrity_check(self):
        damaged = ['*** in database main ***', 'Page 4: btreeInitPage() returns error code 11']
        with mock.patch.object(preflight, 'sqlite_integrity_check', return_value=damaged):
            report = self._run()

        self.assertFalse(report['ok'])
        entry = check_by_name(report, 'integrity_check')
        self.assertEqual(entry['status'], preflight.FAILED)
        self.assertIn('btreeInitPage', entry['details'])
        self.assertIn('integrity_check', report['failures'])

    def test_an_unreadable_database_becomes_a_reported_failure(self):
        from django.db import DatabaseError

        with mock.patch.object(
            preflight, 'sqlite_integrity_check', side_effect=DatabaseError('database disk image is malformed')
        ):
            report = self._run()

        self.assertFalse(report['ok'])
        self.assertIn('malformed', check_by_name(report, 'integrity_check')['details'])

    def test_missing_attachment_files_are_detected(self):
        attachment = self.add_attachment()
        (self.media_root / attachment.file.name).unlink()

        report = self._run()

        self.assertFalse(report['ok'])
        entry = check_by_name(report, 'attachment_files')
        self.assertEqual(entry['status'], preflight.FAILED)
        self.assertIn(str(attachment.pk), entry['details'])

    def test_a_wrong_declared_attachment_size_is_detected(self):
        attachment = self.add_attachment(content=b'four')
        Act.objects.filter(pk=self.act.pk).exists()  # keep the act referenced
        attachment.file_size = 999
        attachment.save(update_fields=['file_size'])

        report = self._run()

        self.assertFalse(report['ok'])
        self.assertEqual(check_by_name(report, 'attachment_sizes')['status'], preflight.FAILED)

    def test_an_unsafe_attachment_path_is_detected(self):
        from acts.models import ActAttachment

        ActAttachment.objects.create(
            act=self.act,
            uploaded_by=self.user,
            file='../../escape.txt',
            original_name='escape.txt',
            file_size=1,
        )

        report = self._run()

        self.assertFalse(report['ok'])
        self.assertEqual(check_by_name(report, 'attachment_paths')['status'], preflight.FAILED)

    def test_a_missing_source_media_directory_is_detected(self):
        missing = self.media_root / 'not-here'

        report = self._run(source_media_root=missing)

        self.assertFalse(report['ok'])
        self.assertEqual(check_by_name(report, 'source_media')['status'], preflight.FAILED)

    def test_the_selected_source_media_is_reported_safely(self):
        separate = Path(tempfile.mkdtemp(prefix='media-copy-'))
        self.addCleanup(shutil.rmtree, separate, ignore_errors=True)

        report = self._run(source_media_root=separate)

        summary = report['summary']['source_media']
        self.assertEqual(summary['name'], separate.name)
        self.assertFalse(summary['is_default_media_root'])
        self.assertNotIn(str(separate), str(report))

    def test_the_working_database_is_refused_without_the_explicit_flag(self):
        with mock.patch.object(preflight, 'is_default_working_database', return_value=True):
            report = self._run(allow_default_database=False)

        self.assertFalse(report['ok'])
        entry = check_by_name(report, 'database_copy')
        self.assertEqual(entry['status'], preflight.FAILED)
        self.assertIn('--allow-default-database', entry['details'])

    def test_the_working_database_is_only_a_warning_with_the_flag(self):
        with mock.patch.object(preflight, 'is_default_working_database', return_value=True):
            report = self._run(allow_default_database=True)

        entry = check_by_name(report, 'database_copy')
        self.assertEqual(entry['status'], preflight.WARNING)
        self.assertTrue(report['ok'], report['failures'])

    def test_the_command_reports_a_failure_as_a_command_error(self):
        attachment = self.add_attachment()
        (self.media_root / attachment.file.name).unlink()

        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with override_settings(MEDIA_ROOT=self.media_root):
                with self.assertRaises(CommandError):
                    call_command(
                        'check_migration_source',
                        '--allow-default-database',
                        '--source-media-root',
                        str(self.media_root),
                    )

    def test_the_command_writes_a_json_report(self):
        target = Path(tempfile.mkdtemp(prefix='preflight-')) / 'report.json'
        self.addCleanup(shutil.rmtree, target.parent, ignore_errors=True)

        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with mock.patch.object(
                preflight, 'get_sqlite_database_path', return_value=self.database_copy
            ):
                with override_settings(MEDIA_ROOT=self.media_root):
                    call_command(
                        'check_migration_source',
                        '--allow-default-database',
                        '--source-media-root',
                        str(self.media_root),
                        '--json-report',
                        str(target),
                    )

        self.assertTrue(target.is_file())
        self.assertIn('"checks"', target.read_text(encoding='utf-8'))


class RealCorruptedSqliteTests(SimpleTestCase):
    """A genuinely damaged file must be rejected by a real command run.

    The check runs in a separate process because Django binds its connection to
    one database file at startup — exactly the reason the rehearsal itself uses
    separate processes.
    """

    def setUp(self):
        self.work_dir = Path(tempfile.mkdtemp(prefix='corrupt-sqlite-'))
        self.addCleanup(shutil.rmtree, self.work_dir, ignore_errors=True)

    def test_pragma_integrity_check_reports_a_damaged_file(self):
        path = self.work_dir / 'damaged.sqlite3'
        with sqlite3.connect(path) as source:
            source.execute('CREATE TABLE demo (id integer primary key, payload text)')
            source.executemany(
                'INSERT INTO demo (payload) VALUES (?)', [(f'value-{i}' * 40,) for i in range(200)]
            )
            source.commit()
        raw = bytearray(path.read_bytes())
        # Keep the 100-byte header intact so the file still opens, then destroy
        # the interior pages the way real disk damage would.
        raw[4096:8192] = b'\x00' * min(4096, max(0, len(raw) - 4096))
        path.write_bytes(bytes(raw))

        try:
            with sqlite3.connect(path) as damaged:
                result = [row[0] for row in damaged.execute('PRAGMA integrity_check').fetchall()]
        except sqlite3.DatabaseError as exc:
            # SQLite may refuse the pragma outright; the source preflight turns
            # that DatabaseError into a reported failure just the same.
            result = [f'DatabaseError: {exc}']

        self.assertNotEqual(result, ['ok'], result)

    def test_the_command_fails_on_a_file_that_is_not_a_database(self):
        path = self.work_dir / 'not-a-database.sqlite3'
        path.write_bytes(b'SQLite format 3\x00' + b'\xff' * 4096)

        completed = subprocess.run(
            [
                sys.executable,
                str(Path(settings.BASE_DIR) / 'manage.py'),
                'check_migration_source',
                '--allow-default-database',
            ],
            cwd=str(settings.BASE_DIR),
            env={
                **_clean_environment(),
                'DATABASE_ENGINE': 'sqlite',
                'SQLITE_DB_PATH': str(path),
                'PYTHONIOENCODING': 'utf-8',
            },
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=300,
        )

        self.assertNotEqual(completed.returncode, 0, completed.stdout + completed.stderr)


def _clean_environment():
    import os

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {'DATABASE_ENGINE', 'SQLITE_DB_PATH', 'MEDIA_ROOT_PATH'}
    }
    return environment
