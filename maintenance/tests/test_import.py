import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from accounts.models import UserProfile
from acts.models import Act
from maintenance import database_transfer as dt
from references.models import ActStatus, DefectType, Operation

from .test_bundle import BundleFixtureMixin


requires_postgresql = unittest.skipUnless(
    connection.vendor == 'postgresql',
    'Импорт и восстановление последовательностей проверяются только на PostgreSQL.',
)


class ImportGuardTests(BundleFixtureMixin, TestCase):
    """Refusals that can be verified without a real PostgreSQL import."""

    def setUp(self):
        self.setUpBundleData()
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with override_settings(MEDIA_ROOT=self.media_root):
                dt.export_bundle(self.bundle_path('source'))
        self.bundle = self.bundle_path('source')

    def test_import_is_refused_on_a_non_postgresql_backend(self):
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with self.assertRaisesMessage(dt.TransferError, 'только на PostgreSQL'):
                dt.check_import_preconditions(self.bundle)

    def test_import_is_refused_while_email_notifications_are_enabled(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=True):
                with self.assertRaisesMessage(dt.TransferError, 'EMAIL_NOTIFICATIONS_ENABLED'):
                    dt.check_import_preconditions(self.bundle)

    def test_import_is_refused_when_transferable_tables_are_not_empty(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
                with self.assertRaisesMessage(dt.TransferError, 'только в пустые переносимые таблицы'):
                    dt.check_import_preconditions(self.bundle)

    def test_refusal_message_separates_seeded_reference_tables(self):
        with mock.patch.object(connection, 'vendor', 'postgresql'):
            with override_settings(EMAIL_NOTIFICATIONS_ENABLED=False):
                with self.assertRaises(dt.TransferError) as ctx:
                    dt.check_import_preconditions(self.bundle)
        message = str(ctx.exception)
        self.assertIn('references.ActStatus', message)
        self.assertIn('prepare_empty_migration_target', message)
        self.assertIn('ничего не удаляет', message)

    def test_find_non_empty_models_reports_populated_tables(self):
        non_empty = dt.find_non_empty_models()
        self.assertIn('acts.Act', non_empty)
        self.assertIn('auth.User', non_empty)

    def test_import_command_reports_a_refusal_as_command_error(self):
        with self.assertRaises(CommandError):
            call_command('import_migration_bundle', '--input', str(self.bundle), '--dry-run')


class RawFixtureSignalTests(TestCase):
    """A serialized User + UserProfile pair must load without a OneToOne clash."""

    def test_raw_load_does_not_create_a_duplicate_profile(self):
        source_user = User.objects.create_user(username='raw_user', password='demo12345')
        profile = UserProfile.objects.get(user=source_user)
        profile.position = 'Инженер'
        profile.save(update_fields=['position'])

        from django.core import serializers

        payload = serializers.serialize(
            'json', [source_user, profile], use_natural_foreign_keys=True
        )
        User.objects.filter(pk=source_user.pk).delete()
        self.assertFalse(UserProfile.objects.filter(user_id=source_user.pk).exists())

        for deserialized in serializers.deserialize('json', payload):
            deserialized.save()

        self.assertEqual(UserProfile.objects.filter(user_id=source_user.pk).count(), 1)
        self.assertEqual(UserProfile.objects.get(user_id=source_user.pk).position, 'Инженер')

    def test_normal_user_creation_still_creates_a_profile(self):
        user = User.objects.create_user(username='normal_user', password='demo12345')
        self.assertTrue(UserProfile.objects.filter(user=user).exists())


class SequenceResetTests(TestCase):
    def test_sequence_reset_is_a_no_op_without_backend_sequences(self):
        result = dt.reset_database_sequences()

        self.assertIn('acts.Act', result['models'])
        if connection.vendor == 'sqlite':
            self.assertEqual(result['statements'], 0)
        else:
            self.assertGreater(result['statements'], 0)


@requires_postgresql
class PostgresImportTests(BundleFixtureMixin, TransactionTestCase):
    """Full dry-run / import / sequence / verification cycle on PostgreSQL."""

    reset_sequences = True

    def setUp(self):
        self.setUpBundleData()
        with mock.patch.object(connection, 'vendor', 'sqlite'):
            with override_settings(MEDIA_ROOT=self.media_root):
                self.add_attachment()
                dt.export_bundle(self.bundle_path('source'))
        self.bundle = self.bundle_path('source')
        self.expected = dt.validate_bundle(self.bundle)

        self.target_media = Path(tempfile.mkdtemp(prefix='target-media-'))
        self.addCleanup(shutil.rmtree, self.target_media, ignore_errors=True)

    def _empty_all_transferable_tables(self):
        for label in reversed(dt.TRANSFERABLE_MODELS):
            dt.apps.get_model(label)._default_manager.all().delete()

    def test_dry_run_changes_neither_database_nor_filesystem(self):
        self._empty_all_transferable_tables()
        with override_settings(MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False):
            validation, actions = dt.plan_import(self.bundle)

        self.assertTrue(actions)
        self.assertEqual(Act.objects.count(), 0)
        self.assertEqual(User.objects.count(), 0)
        self.assertEqual(list(self.target_media.iterdir()), [])
        self.assertEqual(validation['record_count'], self.expected['record_count'])

    def test_import_restores_data_media_and_sequences(self):
        self._empty_all_transferable_tables()
        with override_settings(MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False):
            result = dt.import_bundle(self.bundle)

            self.assertEqual(result['loaded'], self.expected['record_count'])
            self.assertEqual(len(result['media']['copied']), self.expected['media_count'])
            self.assertGreater(result['sequences']['statements'], 0)

            report = dt.verify_against_bundle(self.bundle)
            self.assertTrue(report['ok'], report['differences'])

    def test_new_rows_after_import_do_not_collide_with_imported_primary_keys(self):
        self._empty_all_transferable_tables()
        with override_settings(MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False):
            dt.import_bundle(self.bundle)

            highest_act_pk = Act.objects.order_by('-pk').values_list('pk', flat=True).first()
            new_act = Act.objects.create(
                created_by=User.objects.get(username='bundle_user'),
                nomenclature='Катушка',
                status=ActStatus.objects.get(code='CREATED_OTK'),
            )

        self.assertGreater(new_act.pk, highest_act_pk)

    def test_import_is_refused_when_media_root_is_not_empty(self):
        self._empty_all_transferable_tables()
        (self.target_media / 'existing.txt').write_text('x', encoding='utf-8')
        with override_settings(MEDIA_ROOT=self.target_media, EMAIL_NOTIFICATIONS_ENABLED=False):
            with self.assertRaisesMessage(dt.TransferError, 'не пуст'):
                dt.check_import_preconditions(self.bundle)
