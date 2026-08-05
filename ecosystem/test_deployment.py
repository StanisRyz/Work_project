"""Health endpoints, deployment system checks and the readiness commands."""

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from ecosystem import checks as deployment_checks


VALID_TEST_KEY = 'x7Qw9zLp2mR4tYv8bN3kJ6hG1sD5fA0cE7uI9oP4rT2wX6yZ8aB3'

# A configuration that satisfies every blocking rule, so a test can switch one
# thing off at a time and see exactly that check fire.
SAFE_PRODUCTION = {
    'IS_PRODUCTION': True,
    'DEBUG': False,
    'SECRET_KEY': VALID_TEST_KEY,
    'ALLOWED_HOSTS': ['quality.example.internal'],
    'CSRF_TRUSTED_ORIGINS': ['https://quality.example.internal'],
    'APP_BASE_URL': 'https://quality.example.internal',
    'SESSION_COOKIE_SECURE': True,
    'CSRF_COOKIE_SECURE': True,
    'SECURE_SSL_REDIRECT': True,
    'DEMO_RESET_REQUESTED': False,
    'EMAIL_NOTIFICATIONS_ENABLED': False,
    'REALTIME_ENABLED': False,
    'BACKUP_POLICY_ACKNOWLEDGED': True,
    'SECURE_HSTS_SECONDS': 31536000,
    'DB_SSLMODE': 'require',
    'DB_STATEMENT_TIMEOUT_MS': 30000,
    'DB_LOCK_TIMEOUT_MS': 10000,
    'DB_IDLE_IN_TRANSACTION_TIMEOUT_MS': 60000,
}


def _ids(messages):
    return {message.id for message in messages}


def _run_checks():
    return deployment_checks.check_deployment_configuration(None)


class DeploymentCheckTests(SimpleTestCase):
    def test_nothing_is_reported_outside_production(self):
        with override_settings(IS_PRODUCTION=False, DEBUG=True):
            self.assertEqual(_run_checks(), [])

    def test_a_safe_production_configuration_reports_nothing(self):
        # PostgreSQL is asserted through the DATABASES setting only; no server
        # is contacted.
        with override_settings(
            **SAFE_PRODUCTION,
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertEqual(_run_checks(), [])

    def test_each_unsafe_setting_produces_its_own_blocking_error(self):
        cases = {
            'ecosystem.E001': {'DEBUG': True},
            'ecosystem.E002': {'ALLOWED_HOSTS': []},
            'ecosystem.E003': {'ALLOWED_HOSTS': ['*']},
            'ecosystem.E004': {'APP_BASE_URL': 'http://quality.example.internal'},
            'ecosystem.E005': {'CSRF_TRUSTED_ORIGINS': []},
            'ecosystem.E006': {'CSRF_TRUSTED_ORIGINS': ['http://quality.example.internal']},
            'ecosystem.E007': {'SESSION_COOKIE_SECURE': False},
            'ecosystem.E008': {'CSRF_COOKIE_SECURE': False},
            'ecosystem.E009': {'SECURE_SSL_REDIRECT': False},
            'ecosystem.E013': {'DEMO_RESET_REQUESTED': True},
        }
        for expected_id, override in cases.items():
            with self.subTest(check=expected_id):
                with override_settings(
                    **{**SAFE_PRODUCTION, **override},
                    DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
                ):
                    self.assertIn(expected_id, _ids(_run_checks()))

    def test_sqlite_is_blocking_in_production(self):
        with override_settings(
            **SAFE_PRODUCTION,
            DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3'}},
        ):
            self.assertIn('ecosystem.E010', _ids(_run_checks()))

    def test_console_email_with_notifications_enabled_is_blocking(self):
        with override_settings(
            **{
                **SAFE_PRODUCTION,
                'EMAIL_NOTIFICATIONS_ENABLED': True,
                'EMAIL_BACKEND': 'django.core.mail.backends.console.EmailBackend',
            },
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertIn('ecosystem.E014', _ids(_run_checks()))

    def test_tls_and_ssl_together_are_blocking(self):
        with override_settings(
            **{
                **SAFE_PRODUCTION,
                'EMAIL_NOTIFICATIONS_ENABLED': True,
                'EMAIL_BACKEND': 'django.core.mail.backends.smtp.EmailBackend',
                'EMAIL_USE_TLS': True,
                'EMAIL_USE_SSL': True,
                'DEFAULT_FROM_EMAIL': 'quality@example.internal',
            },
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertIn('ecosystem.E015', _ids(_run_checks()))

    def test_disabled_email_needs_no_smtp_configuration(self):
        with override_settings(
            **{**SAFE_PRODUCTION, 'EMAIL_NOTIFICATIONS_ENABLED': False, 'DEFAULT_FROM_EMAIL': ''},
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            reported = _ids(_run_checks())

        self.assertNotIn('ecosystem.E014', reported)
        self.assertNotIn('ecosystem.E016', reported)

    def test_realtime_enabled_with_the_noop_publisher_is_blocking(self):
        with override_settings(
            **{
                **SAFE_PRODUCTION,
                'REALTIME_ENABLED': True,
                'REALTIME_PUBLISHER_BACKEND': 'realtime.backends.NoopRealtimePublisher',
            },
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertIn('ecosystem.E018', _ids(_run_checks()))

    def test_a_missing_asgi_application_is_blocking_when_realtime_is_on(self):
        with override_settings(
            **{
                **SAFE_PRODUCTION,
                'REALTIME_ENABLED': True,
                'REALTIME_PUBLISHER_BACKEND': 'realtime.backends.RedisRealtimePublisher',
                'ASGI_APPLICATION': '',
            },
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertIn('ecosystem.E019', _ids(_run_checks()))

    def test_warnings_cover_hsts_sslmode_timeouts_redis_and_backup(self):
        cases = {
            'ecosystem.W001': {'SECURE_HSTS_SECONDS': 0},
            'ecosystem.W003': {'DB_SSLMODE': 'prefer'},
            'ecosystem.W004': {'DB_STATEMENT_TIMEOUT_MS': 0},
            'ecosystem.W006': {'BACKUP_POLICY_ACKNOWLEDGED': False},
        }
        for expected_id, override in cases.items():
            with self.subTest(check=expected_id):
                with override_settings(
                    **{**SAFE_PRODUCTION, **override},
                    DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
                ):
                    self.assertIn(expected_id, _ids(_run_checks()))

    def test_plain_redis_is_a_warning_unless_the_network_is_declared_trusted(self):
        realtime = {
            'REALTIME_ENABLED': True,
            'REALTIME_PUBLISHER_BACKEND': 'realtime.backends.RedisRealtimePublisher',
            'REALTIME_REDIS_URL': 'redis://redis.internal:6379/0',
        }
        with override_settings(
            **{**SAFE_PRODUCTION, **realtime, 'REDIS_NETWORK_IS_TRUSTED': False},
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertIn('ecosystem.W005', _ids(_run_checks()))

        with override_settings(
            **{**SAFE_PRODUCTION, **realtime, 'REDIS_NETWORK_IS_TRUSTED': True},
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            self.assertNotIn('ecosystem.W005', _ids(_run_checks()))

    def test_no_check_message_ever_contains_a_secret(self):
        with override_settings(
            **{
                **SAFE_PRODUCTION,
                'DEBUG': True,
                'SECRET_KEY': 'super-secret-signing-key-value',
                'ALLOWED_HOSTS': ['secret-host.internal'],
                'REALTIME_REDIS_URL': 'redis://user:redis-password@redis.internal:6379/0',
                'EMAIL_HOST_PASSWORD': 'smtp-password',
            },
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            rendered = ' '.join(f'{m.msg} {m.hint}' for m in _run_checks())

        for secret in (
            'super-secret-signing-key-value',
            'redis-password',
            'smtp-password',
            'secret-host.internal',
        ):
            self.assertNotIn(secret, rendered)


class HealthEndpointTests(TestCase):
    def test_liveness_returns_ok_without_touching_the_database(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse('health_live'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ok'})
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(len(queries), 0, 'liveness must not query the database')

    def test_liveness_accepts_head(self):
        self.assertEqual(self.client.head(reverse('health_live')).status_code, 200)

    def test_liveness_rejects_other_methods(self):
        for method in ('post', 'put', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(reverse('health_live'))
                self.assertEqual(response.status_code, 405)

    def test_readiness_rejects_other_methods(self):
        for method in ('post', 'put', 'delete'):
            with self.subTest(method=method):
                response = getattr(self.client, method)(reverse('health_ready'))
                self.assertEqual(response.status_code, 405)

    def test_readiness_succeeds_on_a_prepared_installation(self):
        response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'status': 'ready'})
        self.assertEqual(response['Cache-Control'], 'no-store')

    def test_readiness_needs_no_user_session(self):
        # No login anywhere in this test class; this makes that explicit.
        self.assertEqual(self.client.get(reverse('health_ready')).status_code, 200)

    def test_an_unreachable_database_reports_unavailable(self):
        with mock.patch(
            'ecosystem.health._check_database', return_value='connection_failed'
        ):
            response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})

    def test_pending_migrations_report_unavailable(self):
        with mock.patch('ecosystem.health._check_migrations', return_value='pending=3'):
            response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 503)

    def test_an_unreachable_redis_reports_unavailable_when_realtime_is_on(self):
        with override_settings(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
        ):
            with mock.patch('realtime.transport.sync_client') as client:
                client.return_value.ping.side_effect = OSError('boom')
                response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 503)

    def test_redis_is_not_probed_when_realtime_is_disabled(self):
        with override_settings(REALTIME_ENABLED=False):
            with mock.patch('realtime.transport.sync_client') as client:
                response = self.client.get(reverse('health_ready'))

        self.assertEqual(response.status_code, 200)
        client.assert_not_called()

    def test_a_failing_probe_never_leaks_details_to_the_caller(self):
        with mock.patch(
            'ecosystem.health._check_database',
            side_effect=RuntimeError('FATAL: password authentication failed for user "quality"'),
        ):
            response = self.client.get(reverse('health_ready'))

        body = response.content.decode()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        for leak in ('password', 'quality', 'FATAL', 'RuntimeError'):
            self.assertNotIn(leak, body)

    def test_readiness_changes_nothing(self):
        from acts.models import Act

        before = Act.objects.count()
        self.client.get(reverse('health_ready'))
        self.assertEqual(Act.objects.count(), before)


class FreshBootstrapCommandTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # Migrations seed only a couple of statuses; the full reference set is
        # what `seed_references` installs, and it is a documented bootstrap
        # step. Without it the command would (correctly) block on missing
        # reference data in every test here.
        call_command('seed_references', stdout=StringIO())

    def _run(self, **options):
        buffer = StringIO()
        try:
            call_command('check_fresh_bootstrap', stdout=buffer, stderr=buffer, **options)
            code = 0
        except SystemExit as exc:
            code = exc.code
        return code, buffer.getvalue()

    def test_sqlite_blocks_a_production_bootstrap(self):
        if connection.vendor == 'postgresql':
            self.skipTest('This asserts the non-PostgreSQL refusal.')

        code, output = self._run()

        self.assertEqual(code, 1)
        self.assertIn('BLOCKING', output)
        self.assertIn('backend', output)

    def test_the_sqlite_override_lets_the_rest_of_the_checks_run(self):
        code, output = self._run(allow_sqlite=True)

        # Reference data is seeded by migrations in the test database.
        self.assertIn('act_statuses', output)
        self.assertIn('migrations', output)
        self.assertIsNotNone(code)

    def test_a_missing_reference_status_is_blocking(self):
        from references.models import TaskStatus

        TaskStatus.objects.filter(code='COMPLETED').delete()

        code, output = self._run(allow_sqlite=True)

        self.assertEqual(code, 1)
        self.assertIn('task_statuses', output)

    def test_synthetic_data_is_blocking(self):
        from acts.models import Act
        from references.models import ActStatus, DefectType, Operation
        from django.contrib.auth.models import User

        Act.objects.create(
            number='PERF-SYNTHETIC-2026-000001',
            created_by=User.objects.create_user('bootstrap_probe'),
            party_number='P-1',
            nomenclature='PERF-SYNTHETIC изделие',
            operation=Operation.objects.first(),
            defect_type=DefectType.objects.first(),
            status=ActStatus.objects.get(code='CREATED_OTK'),
            description='PERF-SYNTHETIC',
        )

        code, output = self._run(allow_sqlite=True)

        self.assertEqual(code, 1)
        self.assertIn('synthetic_data', output)

    def test_a_demo_administrator_is_blocking_unless_explicitly_allowed(self):
        from django.contrib.auth.models import User

        User.objects.create_user('admin_user', password='demo12345')

        code, output = self._run(allow_sqlite=True)
        self.assertEqual(code, 1)
        self.assertIn('demo_admin', output)

        code, _ = self._run(allow_sqlite=True, allow_demo_admin=True)
        self.assertEqual(code, 0)

    def test_the_demo_reset_flag_is_blocking(self):
        with override_settings(DEMO_RESET_REQUESTED=True):
            code, output = self._run(allow_sqlite=True)

        self.assertEqual(code, 1)
        self.assertIn('demo_reset', output)

    def test_existing_working_data_is_only_a_warning(self):
        from django.contrib.auth.models import User

        User.objects.create_user('operator_one')
        User.objects.create_user('operator_two')

        code, output = self._run(allow_sqlite=True)

        self.assertEqual(code, 0, 'the command must stay re-runnable after go-live')
        self.assertIn('existing_data', output)
        self.assertIn('WARNING', output)

    def test_the_output_never_contains_a_username(self):
        from django.contrib.auth.models import User

        User.objects.create_user('very_identifiable_person')

        _code, output = self._run(allow_sqlite=True)

        self.assertNotIn('very_identifiable_person', output)

    def test_the_command_changes_nothing(self):
        from django.contrib.auth.models import User

        from acts.models import Act

        before = (Act.objects.count(), User.objects.count())
        self._run(allow_sqlite=True)
        self.assertEqual((Act.objects.count(), User.objects.count()), before)


class ProductionReadinessCommandTests(TestCase):
    def _run(self, **options):
        buffer = StringIO()
        try:
            call_command('check_production_readiness', stdout=buffer, stderr=buffer, **options)
            code = 0
        except SystemExit as exc:
            code = exc.code
        return code, buffer.getvalue()

    def test_it_reports_every_section(self):
        _code, output = self._run()

        for section in (
            'django_checks',
            'database_backend',
            'realtime_transport',
            'static_root',
            'media_isolation',
            'demo_reset',
            'email',
            'backup_policy',
        ):
            self.assertIn(section, output)

    def test_redis_is_only_probed_when_realtime_is_enabled(self):
        with override_settings(REALTIME_ENABLED=False):
            with mock.patch('realtime.transport.sync_client') as client:
                _code, output = self._run()

        client.assert_not_called()
        self.assertIn('Real-time выключен', output)

    def test_an_unreachable_redis_is_blocking_when_realtime_is_enabled(self):
        with override_settings(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
        ):
            with mock.patch('realtime.transport.sync_client') as client:
                client.return_value.ping.side_effect = OSError('boom')
                code, output = self._run()

        self.assertEqual(code, 1)
        self.assertIn('realtime_transport', output)

    def test_the_json_report_is_safe_and_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'readiness.json'
            self._run(json_report=str(path))
            raw = path.read_text(encoding='utf-8')
            report = json.loads(raw)

        self.assertEqual(report['schema_version'], 1)
        self.assertIn('summary', report)
        self.assertTrue(report['results'])
        for item in report['results']:
            self.assertIn(item['status'], {'ok', 'warning', 'blocking'})
        for leak in ('django-insecure', 'DB_PASSWORD', 'redis://', 'admin_user'):
            self.assertNotIn(leak, raw)

    def test_a_blocking_result_exits_non_zero(self):
        with override_settings(
            EMAIL_NOTIFICATIONS_ENABLED=True,
            EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend',
        ):
            code, output = self._run()

        self.assertEqual(code, 1)
        self.assertIn('BLOCKING', output)

    def test_the_command_changes_nothing(self):
        from acts.models import Act

        before = Act.objects.count()
        self._run()
        self.assertEqual(Act.objects.count(), before)


class EmailConfigurationCheckTests(SimpleTestCase):
    """Configuration only — no SMTP connection is ever attempted."""

    def _email_problems(self, **overrides):
        with override_settings(
            **{**SAFE_PRODUCTION, **overrides},
            DATABASES={'default': {'ENGINE': 'django.db.backends.postgresql'}},
        ):
            return _ids(_run_checks())

    def test_disabled_email_requires_nothing(self):
        reported = self._email_problems(EMAIL_NOTIFICATIONS_ENABLED=False)

        for check_id in ('ecosystem.E014', 'ecosystem.E015', 'ecosystem.E016', 'ecosystem.E017'):
            self.assertNotIn(check_id, reported)

    def test_an_empty_sender_is_blocking_when_email_is_enabled(self):
        reported = self._email_problems(
            EMAIL_NOTIFICATIONS_ENABLED=True,
            EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
            DEFAULT_FROM_EMAIL='',
        )

        self.assertIn('ecosystem.E016', reported)

    def test_non_positive_queue_settings_are_blocking(self):
        reported = self._email_problems(
            EMAIL_NOTIFICATIONS_ENABLED=True,
            EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
            DEFAULT_FROM_EMAIL='quality@example.internal',
            EMAIL_NOTIFICATION_BATCH_SIZE=0,
        )

        self.assertIn('ecosystem.E017', reported)

    def test_a_correct_production_smtp_configuration_passes(self):
        reported = self._email_problems(
            EMAIL_NOTIFICATIONS_ENABLED=True,
            EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend',
            EMAIL_USE_TLS=True,
            EMAIL_USE_SSL=False,
            DEFAULT_FROM_EMAIL='quality@example.internal',
        )

        for check_id in ('ecosystem.E014', 'ecosystem.E015', 'ecosystem.E016', 'ecosystem.E017'):
            self.assertNotIn(check_id, reported)
