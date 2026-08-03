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
)


class DatabaseConfigurationTests(SimpleTestCase):
    """Reloads ecosystem.settings under a controlled environment to test
    DATABASES selection in isolation. Never requires a running PostgreSQL
    server: postgresql configuration is only ever assembled/validated, not
    connected to."""

    def setUp(self):
        self._original_environ = {key: os.environ.get(key) for key in DB_ENV_KEYS}
        for key in DB_ENV_KEYS:
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

    def test_default_engine_is_sqlite(self):
        module = importlib.reload(settings_module)

        self.assertEqual(module.DATABASE_ENGINE, 'sqlite')
        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')
        self.assertEqual(module.DATABASES['default']['NAME'], module.BASE_DIR / 'db.sqlite3')

    def test_sqlite_does_not_require_postgresql_variables(self):
        self._set_env(DATABASE_ENGINE='sqlite')

        module = importlib.reload(settings_module)

        self.assertEqual(module.DATABASES['default']['ENGINE'], 'django.db.backends.sqlite3')

    def test_postgresql_variables_build_expected_databases_dict(self):
        self._set_env(
            DATABASE_ENGINE='PostgreSQL',
            DB_NAME='quality_ecosystem',
            DB_USER='quality_user',
            DB_PASSWORD='secret',
            DB_HOST='db.internal',
            DB_PORT='6543',
            DB_CONN_MAX_AGE='60',
            DB_CONN_HEALTH_CHECKS='true',
        )

        module = importlib.reload(settings_module)

        self.assertEqual(
            module.DATABASES['default'],
            {
                'ENGINE': 'django.db.backends.postgresql',
                'NAME': 'quality_ecosystem',
                'USER': 'quality_user',
                'PASSWORD': 'secret',
                'HOST': 'db.internal',
                'PORT': 6543,
                'CONN_MAX_AGE': 60,
                'CONN_HEALTH_CHECKS': True,
            },
        )

    def test_postgresql_defaults_host_port_and_connection_settings(self):
        self._set_env(DATABASE_ENGINE='postgresql', DB_NAME='n', DB_USER='u', DB_PASSWORD='p')

        module = importlib.reload(settings_module)

        db = module.DATABASES['default']
        self.assertEqual(db['HOST'], '127.0.0.1')
        self.assertEqual(db['PORT'], 5432)
        self.assertEqual(db['CONN_MAX_AGE'], 0)
        self.assertFalse(db['CONN_HEALTH_CHECKS'])

    def test_postgresql_missing_required_variable_raises_improperly_configured(self):
        required = {'DB_NAME': 'n', 'DB_USER': 'u', 'DB_PASSWORD': 'p'}
        for missing_key in required:
            with self.subTest(missing=missing_key):
                for key in DB_ENV_KEYS:
                    os.environ.pop(key, None)
                self._set_env(DATABASE_ENGINE='postgresql', **{
                    key: value for key, value in required.items() if key != missing_key
                })

                with self.assertRaises(ImproperlyConfigured) as ctx:
                    importlib.reload(settings_module)
                self.assertIn(missing_key, str(ctx.exception))

    def test_unsupported_database_engine_is_rejected(self):
        self._set_env(DATABASE_ENGINE='mysql')

        with self.assertRaises(ImproperlyConfigured) as ctx:
            importlib.reload(settings_module)

        message = str(ctx.exception)
        self.assertIn('mysql', message)
        self.assertIn('sqlite', message)
        self.assertIn('postgresql', message)

    def test_invalid_db_conn_max_age_is_rejected_with_clear_error(self):
        self._set_env(
            DATABASE_ENGINE='postgresql',
            DB_NAME='n',
            DB_USER='u',
            DB_PASSWORD='p',
            DB_CONN_MAX_AGE='not-a-number',
        )

        with self.assertRaises(ImproperlyConfigured) as ctx:
            importlib.reload(settings_module)

        self.assertIn('DB_CONN_MAX_AGE', str(ctx.exception))
