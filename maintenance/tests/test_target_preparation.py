from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase

from maintenance import database_transfer as dt
from maintenance import target_preparation as tp
from references.models import ActStatus, TaskStatus


class TargetPreparationTests(TestCase):
    """`prepare_empty_migration_target` may only remove migration-seeded rows.

    The vendor is patched to PostgreSQL because the guard is a backend check,
    while the narrow delete itself is plain ORM work that behaves identically
    on the SQLite test database.
    """

    def _seed_migration_rows(self):
        for code in dt.MIGRATION_SEEDED_ROWS['references.ActStatus']:
            ActStatus.objects.update_or_create(code=code, defaults={'name': code})
        for code in dt.MIGRATION_SEEDED_ROWS['references.TaskStatus']:
            TaskStatus.objects.update_or_create(code=code, defaults={'name': code})

    def _run(self, **kwargs):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            return tp.prepare_empty_target(**kwargs)

    def test_the_command_is_refused_on_a_non_postgresql_backend(self):
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with self.assertRaisesMessage(dt.TransferError, 'только на PostgreSQL'):
                tp.prepare_empty_target()

    def test_dry_run_changes_nothing(self):
        self._seed_migration_rows()
        before_acts = ActStatus.objects.count()
        before_tasks = TaskStatus.objects.count()

        result = self._run()

        self.assertTrue(result['dry_run'])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(ActStatus.objects.count(), before_acts)
        self.assertEqual(TaskStatus.objects.count(), before_tasks)
        # Every migration-seeded model present in the target is planned —
        # read from the constant, so adding one seeded model does not silently
        # narrow what this test checks.
        planned = {entry['model'] for entry in result['planned']}
        self.assertEqual(planned, set(dt.MIGRATION_SEEDED_MODELS))

    def test_execute_removes_only_the_listed_reference_codes(self):
        self._seed_migration_rows()

        result = self._run(execute=True, confirmation=tp.CONFIRMATION_PHRASE)

        self.assertFalse(result['dry_run'])
        self.assertTrue(result['ready'], result['blocking'])
        self.assertFalse(ActStatus.objects.exists())
        self.assertFalse(TaskStatus.objects.exists())
        deleted = {entry['model'] for entry in result['deleted']}
        self.assertEqual(deleted, set(dt.MIGRATION_SEEDED_MODELS))

    def test_execute_requires_the_exact_confirmation(self):
        self._seed_migration_rows()

        with self.assertRaisesMessage(dt.TransferError, 'точного подтверждения'):
            self._run(execute=True, confirmation='да')

        self.assertTrue(ActStatus.objects.exists())

    def test_user_data_blocks_the_preparation_and_nothing_is_deleted(self):
        self._seed_migration_rows()
        User.objects.create_user(username='target_user', password='demo12345')

        result = self._run(execute=True, confirmation=tp.CONFIRMATION_PHRASE)

        self.assertTrue(result['blocking'])
        self.assertEqual(result['deleted'], [])
        self.assertTrue(User.objects.filter(username='target_user').exists())
        self.assertTrue(ActStatus.objects.exists())
        self.assertTrue(any('auth.User' in problem for problem in result['blocking']))

    def test_unexpected_reference_rows_block_the_preparation(self):
        self._seed_migration_rows()
        ActStatus.objects.create(code='CUSTOM_STATUS', name='Свой статус')

        result = self._run(execute=True, confirmation=tp.CONFIRMATION_PHRASE)

        self.assertTrue(result['blocking'])
        self.assertEqual(result['deleted'], [])
        self.assertTrue(ActStatus.objects.filter(code='CUSTOM_STATUS').exists())

    def test_the_command_lists_affected_tables_in_dry_run(self):
        self._seed_migration_rows()

        with mock.patch.object(connection, 'vendor', 'postgresql'):
            call_command('prepare_empty_migration_target')

        self.assertTrue(ActStatus.objects.exists())

    def test_the_command_refuses_when_user_data_is_present(self):
        self._seed_migration_rows()
        User.objects.create_user(username='blocking_user', password='demo12345')

        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with self.assertRaises(CommandError):
                call_command('prepare_empty_migration_target')

        self.assertTrue(User.objects.filter(username='blocking_user').exists())
