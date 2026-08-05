import logging
import os
import sys

from django.apps import AppConfig


logger = logging.getLogger('ecosystem.startup')

# Management commands that are tooling, not a served process: a startup banner
# in front of their output is noise, and `check`/`makemigrations` in particular
# run constantly during development.
_QUIET_COMMANDS = frozenset(
    {
        'check',
        'check_fresh_bootstrap',
        'check_logging',
        'check_migration_source',
        'check_migration_target',
        'check_production_readiness',
        'check_realtime_transport',
        'collectstatic',
        'createsuperuser',
        'makemigrations',
        'migrate',
        'seed_references',
        'shell',
        'showmigrations',
        'test',
    }
)

_startup_logged = False


class EcosystemConfig(AppConfig):
    """The project package itself, registered so its checks are discovered.

    It defines no models and therefore has no migrations. Its jobs are to
    import the deployment system checks — which need to run inside the ordinary
    `python manage.py check` so an unsafe production configuration stops a
    deployment before the process starts serving — and to record one startup
    line describing how this process is configured.
    """

    name = 'ecosystem'
    verbose_name = 'Конфигурация развёртывания'

    def ready(self):
        from . import checks  # noqa: F401

        _log_application_started()


def _log_application_started():
    """One `application.started` line per served process.

    Deliberately offline: it reports what the *configuration* says, without
    connecting to PostgreSQL, Redis or SMTP — a startup banner must never be
    the thing that makes a process fail to start. It carries no host, path,
    credential or secret, only flags and the safe release marker.
    """
    global _startup_logged
    if _startup_logged:
        return
    if _current_command() in _QUIET_COMMANDS:
        return

    from django.conf import settings

    from .logging_utils import log_event

    _startup_logged = True
    log_event(
        logger,
        'INFO',
        'application.started',
        app_env=getattr(settings, 'APP_ENV', ''),
        release=getattr(settings, 'APP_RELEASE', '') or None,
        # From the configured ENGINE string, so nothing is connected to.
        database=_database_kind(settings),
        realtime_enabled=bool(getattr(settings, 'REALTIME_ENABLED', False)),
        email_enabled=bool(getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False)),
        log_to_file=bool(getattr(settings, 'LOG_TO_FILE', False)),
        log_to_console=bool(getattr(settings, 'LOG_TO_CONSOLE', True)),
        log_level=getattr(settings, 'LOG_LEVEL', ''),
        pid=os.getpid(),
    )


def _current_command():
    """The management subcommand being run, or None for a served process."""
    argv = sys.argv
    if len(argv) < 2 or not argv[0].endswith(('manage.py', 'django-admin')):
        return None
    return argv[1]


def _database_kind(settings):
    engine = str((settings.DATABASES.get('default') or {}).get('ENGINE', ''))
    if 'postgresql' in engine:
        return 'postgresql'
    if 'sqlite' in engine:
        return 'sqlite'
    return 'other'
