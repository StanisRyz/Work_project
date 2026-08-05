import importlib
import os

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from ecosystem import settings as settings_module

DB_ENV_KEYS = (
    'DATABASE_ENGINE',
    'DB_NAME',
    'DB_USER',
    'DB_PASSWORD',
    'DB_HOST',
    'DB_PORT',
    'DB_CONN_MAX_AGE',
    'DB_CONN_HEALTH_CHECKS',
    'DB_SSLMODE',
    'DB_APPLICATION_NAME',
    'DB_STATEMENT_TIMEOUT_MS',
    'DB_LOCK_TIMEOUT_MS',
    'DB_IDLE_IN_TRANSACTION_TIMEOUT_MS',
)

DEPLOYMENT_ENV_KEYS = (
    'APP_ENV',
    'SECRET_KEY',
    'DEBUG',
    'ALLOWED_HOSTS',
    'CSRF_TRUSTED_ORIGINS',
    'APP_BASE_URL',
    'SESSION_COOKIE_SECURE',
    'CSRF_COOKIE_SECURE',
    'SESSION_COOKIE_SAMESITE',
    'CSRF_COOKIE_SAMESITE',
    'SECURE_SSL_REDIRECT',
    'SECURE_HSTS_SECONDS',
    'TRUST_X_FORWARDED_PROTO',
    'X_FRAME_OPTIONS',
    'STATIC_ROOT_PATH',
    'ENABLE_DEMO_RESET',
    'BACKUP_POLICY_ACKNOWLEDGED',
)

# A key that satisfies the production rules: long enough, not the published
# development key, not a placeholder. Test-only, never used anywhere real.
VALID_TEST_KEY = 'x7Qw9zLp2mR4tYv8bN3kJ6hG1sD5fA0cE7uI9oP4rT2wX6yZ8aB3'

PRODUCTION_BASE = {
    'APP_ENV': 'production',
    'DEBUG': 'false',
    'SECRET_KEY': VALID_TEST_KEY,
    'ALLOWED_HOSTS': 'quality.example.internal',
    'CSRF_TRUSTED_ORIGINS': 'https://quality.example.internal',
    'APP_BASE_URL': 'https://quality.example.internal',
    'DATABASE_ENGINE': 'postgresql',
    'DB_NAME': 'n',
    'DB_USER': 'u',
    'DB_PASSWORD': 'p',
}


class SettingsReloadMixin:
    """Reload `ecosystem.settings` under a controlled environment.

    Never requires a running PostgreSQL: a production configuration is only
    ever assembled and validated here, never connected to.
    """

    managed_keys = DB_ENV_KEYS + DEPLOYMENT_ENV_KEYS

    def setUp(self):
        self._original_environ = {key: os.environ.get(key) for key in self.managed_keys}
        for key in self.managed_keys:
            os.environ.pop(key, None)
        self.addCleanup(self._restore_environ)

    def _restore_environ(self):
        for key, value in self._original_environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(settings_module)

    def _set_env(self, **kwargs):
        for key, value in kwargs.items():
            os.environ[key] = value

    def _reload_with(self, **overrides):
        for key in self.managed_keys:
            os.environ.pop(key, None)
        self._set_env(**overrides)
        return importlib.reload(settings_module)


class AppEnvTests(SettingsReloadMixin, SimpleTestCase):
    def test_the_default_environment_is_development(self):
        module = self._reload_with()

        self.assertEqual(module.APP_ENV, 'development')
        self.assertTrue(module.IS_DEVELOPMENT)
        self.assertFalse(module.IS_PRODUCTION)
        self.assertFalse(module.IS_TEST)

    def test_development_defaults_run_without_any_configuration(self):
        module = self._reload_with()

        self.assertTrue(module.DEBUG)
        self.assertEqual(module.SECRET_KEY, module.DEVELOPMENT_SECRET_KEY)
        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertFalse(module.SESSION_COOKIE_SECURE)

    def test_an_unknown_environment_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(APP_ENV='staging')

        message = str(ctx.exception)
        self.assertIn('staging', message)
        self.assertIn('development', message)
        self.assertIn('production', message)

    def test_the_test_environment_is_recognised(self):
        module = self._reload_with(APP_ENV='test')

        self.assertTrue(module.IS_TEST)
        self.assertFalse(module.IS_PRODUCTION)


class ProductionSettingsTests(SettingsReloadMixin, SimpleTestCase):
    def test_a_valid_production_configuration_loads(self):
        module = self._reload_with(**PRODUCTION_BASE)

        self.assertTrue(module.IS_PRODUCTION)
        self.assertFalse(module.DEBUG)
        self.assertEqual(module.ALLOWED_HOSTS, ['quality.example.internal'])
        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.postgresql')
        # Secure by default, without needing to be spelled out.
        self.assertTrue(module.SESSION_COOKIE_SECURE)
        self.assertTrue(module.CSRF_COOKIE_SECURE)
        self.assertTrue(module.SECURE_SSL_REDIRECT)
        self.assertTrue(module.SECURE_CONTENT_TYPE_NOSNIFF)
        self.assertEqual(module.X_FRAME_OPTIONS, 'DENY')

    def test_production_requires_a_secret_key(self):
        environment = {key: value for key, value in PRODUCTION_BASE.items() if key != 'SECRET_KEY'}

        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**environment)

        self.assertIn('SECRET_KEY', str(ctx.exception))

    def test_production_rejects_the_published_development_key(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'SECRET_KEY': settings_module.DEVELOPMENT_SECRET_KEY})

        self.assertIn('development key', str(ctx.exception))

    def test_production_rejects_a_short_key(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'SECRET_KEY': 'Zq7Kp2'})

        self.assertIn('50', str(ctx.exception))

    def test_production_rejects_an_obvious_placeholder_key(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'SECRET_KEY': 'changeme'})

        self.assertIn('placeholder', str(ctx.exception))

    def test_no_error_message_ever_contains_the_key_itself(self):
        # Values chosen so they are not substrings of any legitimate wording in
        # the message — otherwise the assertion would pass or fail by accident.
        for secret in ('Zq7Kp2Vx', settings_module.DEVELOPMENT_SECRET_KEY):
            with self.subTest(case=secret[:12]):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self._reload_with(**{**PRODUCTION_BASE, 'SECRET_KEY': secret})
                self.assertNotIn(secret, str(ctx.exception))

    def test_production_rejects_debug(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'DEBUG': 'true'})

        self.assertIn('DEBUG', str(ctx.exception))

    def test_production_rejects_sqlite(self):
        environment = {**PRODUCTION_BASE, 'DATABASE_ENGINE': 'sqlite'}
        environment.pop('DB_NAME')
        environment.pop('DB_USER')
        environment.pop('DB_PASSWORD')

        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**environment)

        self.assertIn('postgresql', str(ctx.exception))

    def test_production_rejects_empty_allowed_hosts(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'ALLOWED_HOSTS': ''})

        self.assertIn('ALLOWED_HOSTS', str(ctx.exception))

    def test_production_rejects_a_wildcard_host(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'ALLOWED_HOSTS': 'quality.example.internal,*'})

        self.assertIn('wildcard', str(ctx.exception))

    def test_production_rejects_an_http_base_url(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'APP_BASE_URL': 'http://quality.example.internal'})

        self.assertIn('APP_BASE_URL', str(ctx.exception))

    def test_production_rejects_missing_csrf_origins(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'CSRF_TRUSTED_ORIGINS': ''})

        self.assertIn('CSRF_TRUSTED_ORIGINS', str(ctx.exception))

    def test_production_rejects_http_csrf_origins(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(
                **{**PRODUCTION_BASE, 'CSRF_TRUSTED_ORIGINS': 'http://quality.example.internal'}
            )

        self.assertIn('CSRF_TRUSTED_ORIGINS', str(ctx.exception))

    def test_demo_reset_is_forced_off_in_production(self):
        module = self._reload_with(**{**PRODUCTION_BASE, 'ENABLE_DEMO_RESET': 'true'})

        # The request is remembered so a deployment check can report it, but the
        # effective flag — the one the URLconf reads — is off.
        self.assertTrue(module.DEMO_RESET_REQUESTED)
        self.assertFalse(module.ENABLE_DEMO_RESET)


class EnvironmentListParserTests(SettingsReloadMixin, SimpleTestCase):
    def test_values_are_split_trimmed_and_deduplicated_in_order(self):
        module = self._reload_with(
            **{**PRODUCTION_BASE, 'ALLOWED_HOSTS': ' b.example.internal , a.example.internal ,, b.example.internal '}
        )

        self.assertEqual(module.ALLOWED_HOSTS, ['b.example.internal', 'a.example.internal'])

    def test_an_unset_variable_falls_back_to_the_default(self):
        module = self._reload_with()

        self.assertIn('127.0.0.1', module.ALLOWED_HOSTS)


class SecuritySettingsTests(SettingsReloadMixin, SimpleTestCase):
    def test_the_proxy_ssl_header_is_absent_unless_the_proxy_is_trusted(self):
        module = self._reload_with(**PRODUCTION_BASE)

        self.assertFalse(hasattr(module, 'SECURE_PROXY_SSL_HEADER'))

    def test_trusting_the_proxy_sets_the_header(self):
        module = self._reload_with(**{**PRODUCTION_BASE, 'TRUST_X_FORWARDED_PROTO': 'true'})

        self.assertEqual(module.SECURE_PROXY_SSL_HEADER, ('HTTP_X_FORWARDED_PROTO', 'https'))

    def test_hsts_is_never_enabled_implicitly(self):
        module = self._reload_with(**PRODUCTION_BASE)

        self.assertEqual(module.SECURE_HSTS_SECONDS, 0)

    def test_an_invalid_samesite_value_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**{**PRODUCTION_BASE, 'SESSION_COOKIE_SAMESITE': 'sometimes'})

        self.assertIn('SESSION_COOKIE_SAMESITE', str(ctx.exception))


class StaticAndMediaSettingsTests(SettingsReloadMixin, SimpleTestCase):
    def test_static_root_has_a_safe_local_default(self):
        module = self._reload_with()

        self.assertEqual(module.STATIC_ROOT, module.BASE_DIR / 'staticfiles')

    def test_static_root_can_be_overridden_and_relative_resolves_against_base_dir(self):
        module = self._reload_with(STATIC_ROOT_PATH='collected')

        self.assertEqual(module.STATIC_ROOT, (module.BASE_DIR / 'collected').resolve())

    def test_static_root_and_media_root_are_different_directories(self):
        module = self._reload_with()

        self.assertNotEqual(str(module.STATIC_ROOT), str(module.MEDIA_ROOT))


class DatabaseConfigurationTests(SettingsReloadMixin, SimpleTestCase):
    def test_default_engine_is_sqlite(self):
        module = self._reload_with()

        self.assertEqual(module.DATABASE_ENGINE, 'sqlite')
        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(module.DATABASES['default']['NAME'], module.BASE_DIR / 'db.sqlite3')

    def test_sqlite_does_not_require_postgresql_variables(self):
        module = self._reload_with(DATABASE_ENGINE='sqlite')

        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')

    def test_sqlite_never_receives_postgresql_options(self):
        module = self._reload_with(DATABASE_ENGINE='sqlite', DB_SSLMODE='require')

        # sslmode, application_name and libpq timeouts are meaningless to
        # SQLite and would raise on connect.
        self.assertNotIn('OPTIONS', module.DATABASES['default'])

    def test_postgresql_variables_build_expected_databases_dict(self):
        module = self._reload_with(
            DATABASE_ENGINE='PostgreSQL',
            DB_NAME='quality_ecosystem',
            DB_USER='quality_user',
            DB_PASSWORD='secret',
            DB_HOST='db.internal',
            DB_PORT='6543',
            DB_CONN_MAX_AGE='60',
            DB_CONN_HEALTH_CHECKS='true',
        )

        database = module.DATABASES['default']
        self.assertEqual(database['ENGINE'], 'django.db.backends.postgresql')
        self.assertEqual(database['NAME'], 'quality_ecosystem')
        self.assertEqual(database['USER'], 'quality_user')
        self.assertEqual(database['PASSWORD'], 'secret')
        self.assertEqual(database['HOST'], 'db.internal')
        self.assertEqual(database['PORT'], 6543)
        self.assertEqual(database['CONN_MAX_AGE'], 60)
        self.assertTrue(database['CONN_HEALTH_CHECKS'])

    def test_postgresql_defaults_host_port_and_connection_settings(self):
        module = self._reload_with(
            DATABASE_ENGINE='postgresql', DB_NAME='n', DB_USER='u', DB_PASSWORD='p'
        )

        db = module.DATABASES['default']
        self.assertEqual(db['HOST'], '127.0.0.1')
        self.assertEqual(db['PORT'], 5432)
        # 0 stays the recommended default under ASGI.
        self.assertEqual(db['CONN_MAX_AGE'], 0)
        self.assertFalse(db['CONN_HEALTH_CHECKS'])

    def test_postgresql_missing_required_variable_raises_improperly_configured(self):
        required = {'DB_NAME': 'n', 'DB_USER': 'u', 'DB_PASSWORD': 'p'}
        for missing_key in required:
            with self.subTest(missing=missing_key):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    self._reload_with(
                        DATABASE_ENGINE='postgresql',
                        **{key: value for key, value in required.items() if key != missing_key},
                    )
                self.assertIn(missing_key, str(ctx.exception))

    def test_unsupported_database_engine_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(DATABASE_ENGINE='mysql')

        message = str(ctx.exception)
        self.assertIn('mysql', message)
        self.assertIn('sqlite', message)
        self.assertIn('postgresql', message)

    def test_invalid_db_conn_max_age_is_rejected_with_clear_error(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(
                DATABASE_ENGINE='postgresql',
                DB_NAME='n',
                DB_USER='u',
                DB_PASSWORD='p',
                DB_CONN_MAX_AGE='not-a-number',
            )

        self.assertIn('DB_CONN_MAX_AGE', str(ctx.exception))


class PostgresqlRuntimeOptionTests(SettingsReloadMixin, SimpleTestCase):
    BASE = {
        'DATABASE_ENGINE': 'postgresql',
        'DB_NAME': 'n',
        'DB_USER': 'u',
        'DB_PASSWORD': 'p',
    }

    def test_defaults_include_sslmode_application_name_and_all_three_timeouts(self):
        module = self._reload_with(**self.BASE)

        options = module.DATABASES['default']['OPTIONS']
        self.assertEqual(options['sslmode'], 'prefer')
        self.assertEqual(options['application_name'], 'quality-ecosystem')
        self.assertIn('statement_timeout=30000', options['options'])
        self.assertIn('lock_timeout=10000', options['options'])
        self.assertIn('idle_in_transaction_session_timeout=60000', options['options'])

    def test_each_value_can_be_configured(self):
        module = self._reload_with(
            **self.BASE,
            DB_SSLMODE='verify-full',
            DB_APPLICATION_NAME='quality-pilot',
            DB_STATEMENT_TIMEOUT_MS='15000',
            DB_LOCK_TIMEOUT_MS='5000',
            DB_IDLE_IN_TRANSACTION_TIMEOUT_MS='45000',
        )

        options = module.DATABASES['default']['OPTIONS']
        self.assertEqual(options['sslmode'], 'verify-full')
        self.assertEqual(options['application_name'], 'quality-pilot')
        self.assertIn('statement_timeout=15000', options['options'])
        self.assertIn('lock_timeout=5000', options['options'])
        self.assertIn('idle_in_transaction_session_timeout=45000', options['options'])

    def test_a_zero_timeout_is_omitted_rather_than_sent_as_zero(self):
        module = self._reload_with(
            **self.BASE,
            DB_STATEMENT_TIMEOUT_MS='0',
            DB_LOCK_TIMEOUT_MS='0',
            DB_IDLE_IN_TRANSACTION_TIMEOUT_MS='0',
        )

        # All three disabled means no `options` string at all.
        self.assertNotIn('options', module.DATABASES['default']['OPTIONS'])

    def test_an_unsupported_sslmode_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**self.BASE, DB_SSLMODE='maybe')

        self.assertIn('DB_SSLMODE', str(ctx.exception))

    def test_a_negative_timeout_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**self.BASE, DB_STATEMENT_TIMEOUT_MS='-1')

        self.assertIn('DB_STATEMENT_TIMEOUT_MS', str(ctx.exception))

    def test_a_non_numeric_timeout_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**self.BASE, DB_LOCK_TIMEOUT_MS='soon')

        self.assertIn('DB_LOCK_TIMEOUT_MS', str(ctx.exception))

    def test_an_empty_application_name_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**self.BASE, DB_APPLICATION_NAME='   ')

        self.assertIn('DB_APPLICATION_NAME', str(ctx.exception))

    def test_an_unsafe_application_name_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(**self.BASE, DB_APPLICATION_NAME="quality' -c evil=1")

        self.assertIn('DB_APPLICATION_NAME', str(ctx.exception))

    def test_no_database_error_message_contains_the_password(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            self._reload_with(
                DATABASE_ENGINE='postgresql',
                DB_NAME='n',
                DB_USER='u',
                DB_PASSWORD='super-secret-password',
                DB_SSLMODE='maybe',
            )

        self.assertNotIn('super-secret-password', str(ctx.exception))
