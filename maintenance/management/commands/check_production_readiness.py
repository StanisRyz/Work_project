"""One consolidated, read-only production readiness report.

Gathers everything a deployment decision needs into a single PASS / WARNING /
BLOCKING list: Django's own deployment checks, the database backend and
version, migration state, reference data, the real-time transport (including a
genuine Redis PING when real-time is on), static and media, the demo reset
flag, email configuration, fresh-bootstrap status and the backup
acknowledgement.

It changes nothing. Unlike the system checks — which are offline by design —
this command *does* reach out to PostgreSQL and Redis, because that is the
question it answers: not «is the configuration sane?» but «is this deployment
actually ready?».

The JSON report carries statuses and short reasons only: no secrets, no full
filesystem paths, no hostnames, no usernames and no business data.
"""

import json
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning as CheckWarning, run_checks
from django.core.management.base import BaseCommand
from django.db import connection

from .check_fresh_bootstrap import (
    BLOCKING,
    PASS,
    WARNING,
    _check_email_configuration,
    run_fresh_bootstrap_checks,
)


class Command(BaseCommand):
    help = (
        'Сводная проверка готовности к production: системные проверки, '
        'PostgreSQL, миграции, справочники, Redis, static/media, demo reset, '
        'email и резервное копирование. Только чтение.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--json-report', help='Куда записать безопасный JSON-отчёт.')
        parser.add_argument(
            '--allow-demo-admin',
            action='store_true',
            help='Передаётся во fresh bootstrap: не считать демо-администратора блокирующим.',
        )

    def handle(self, *args, **options):
        results = []
        results.extend(_django_checks())
        results.extend(_database())
        results.extend(_reference_and_bootstrap(options['allow_demo_admin']))
        results.extend(_realtime())
        results.extend(_static_and_media())
        results.extend(_demo_reset())
        results.extend(_email())
        results.extend(_backup())

        blocking = [item for item in results if item['status'] == BLOCKING]
        warnings = [item for item in results if item['status'] == WARNING]

        self.stdout.write(f'Окружение: APP_ENV={getattr(settings, "APP_ENV", "?")}')
        self.stdout.write('')
        for item in results:
            style = self.style.ERROR if item['status'] == BLOCKING else (
                self.style.WARNING if item['status'] == WARNING else self.style.SUCCESS
            )
            label = {PASS: 'PASS', WARNING: 'WARNING', BLOCKING: 'BLOCKING'}[item['status']]
            self.stdout.write(f'  [{style(label):<8}] {item["check"]}: {item["detail"]}')

        self.stdout.write('')
        self.stdout.write(
            f'Итог: PASS {len(results) - len(blocking) - len(warnings)}, '
            f'WARNING {len(warnings)}, BLOCKING {len(blocking)}.'
        )

        if options['json_report']:
            destination = Path(options['json_report'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            report = {
                'schema_version': 1,
                'app_env': getattr(settings, 'APP_ENV', ''),
                'database_vendor': connection.vendor,
                'summary': {
                    'pass': len(results) - len(blocking) - len(warnings),
                    'warning': len(warnings),
                    'blocking': len(blocking),
                },
                'results': results,
            }
            destination.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            self.stdout.write(f'JSON-отчёт записан: {destination}')

        if blocking:
            self.stdout.write('')
            self.stdout.write(self.style.ERROR('Развёртывание невозможно: есть BLOCKING.'))
            raise SystemExit(1)
        return None


def _result(check, status, detail):
    return {'check': check, 'status': status, 'detail': detail}


# -- sections ---------------------------------------------------------------

def _django_checks():
    """Django's own system and deployment checks, summarised safely."""
    try:
        messages = run_checks(include_deployment_checks=True)
    except Exception as exc:  # noqa: BLE001
        return [_result('django_checks', BLOCKING, f'Не удалось выполнить ({type(exc).__name__}).')]

    errors = [message for message in messages if isinstance(message, Error)]
    warnings = [message for message in messages if isinstance(message, CheckWarning)]
    results = []
    if errors:
        # Only the stable check ids — never the message bodies, which may name
        # settings values.
        ids = ', '.join(sorted({message.id or '?' for message in errors}))
        results.append(_result('django_checks', BLOCKING, f'Ошибки проверок: {ids}.'))
    else:
        results.append(_result('django_checks', PASS, 'Ошибок нет.'))
    if warnings:
        ids = ', '.join(sorted({message.id or '?' for message in warnings}))
        results.append(_result('django_check_warnings', WARNING, f'Предупреждения: {ids}.'))
    return results


def _database():
    results = []
    vendor = connection.vendor
    if vendor != 'postgresql':
        status = BLOCKING if getattr(settings, 'IS_PRODUCTION', False) else WARNING
        results.append(_result('database_backend', status, f'{vendor}, ожидается postgresql.'))
        return results

    results.append(_result('database_backend', PASS, 'PostgreSQL.'))
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW server_version')
            version = cursor.fetchone()[0]
        results.append(_result('database_version', PASS, f'Версия сервера: {version}.'))
    except Exception as exc:  # noqa: BLE001
        results.append(
            _result('database_version', BLOCKING, f'Нет соединения ({type(exc).__name__}).')
        )
    return results


def _reference_and_bootstrap(allow_demo_admin):
    """Reuse the fresh-bootstrap checks rather than re-deriving them.

    `realtime`, `static_root`/`media_root`, `demo_reset` and `email` are
    excluded here: this command reports each of those itself, in more depth
    (a real Redis PING, a `MEDIA_ROOT`/`STATIC_ROOT` isolation comparison, an
    `IS_PRODUCTION`-aware demo-reset severity). Excluding them from the shared
    core — rather than filtering duplicate lines out of the final report —
    keeps every logical check appearing exactly once without ever risking that
    a stricter bootstrap result gets silently dropped in favour of a looser one.
    """
    try:
        return run_fresh_bootstrap_checks(
            allow_demo_admin=allow_demo_admin,
            allow_sqlite=not getattr(settings, 'IS_PRODUCTION', False),
            include_realtime=False,
            include_static=False,
            include_media=False,
            include_demo_reset=False,
            include_email=False,
        )
    except Exception as exc:  # noqa: BLE001
        return [_result('fresh_bootstrap', BLOCKING, f'Не удалось выполнить ({type(exc).__name__}).')]


def _realtime():
    if not getattr(settings, 'REALTIME_ENABLED', False):
        return [_result('realtime_transport', PASS, 'Real-time выключен — Redis не требуется.')]

    publisher = getattr(settings, 'REALTIME_PUBLISHER_BACKEND', '')
    if publisher.endswith('NoopRealtimePublisher'):
        return [
            _result('realtime_transport', BLOCKING, 'Включён real-time с publisher без транспорта.')
        ]

    # A real PING — this command is allowed to touch the network.
    try:
        from realtime.transport import redis_exception_types, sync_client

        sync_client().ping()
    except Exception as exc:  # noqa: BLE001 - never leak the URL or credentials
        return [
            _result('realtime_transport', BLOCKING, f'Redis не отвечает на PING ({type(exc).__name__}).')
        ]
    return [_result('realtime_transport', PASS, 'Redis отвечает на PING.')]


def _static_and_media():
    results = []
    static_root = getattr(settings, 'STATIC_ROOT', None)
    if not static_root:
        results.append(_result('static_root', BLOCKING, 'STATIC_ROOT не задан.'))
    elif not Path(static_root).is_dir():
        results.append(
            _result('static_root', WARNING, 'Каталог не собран — выполните collectstatic --noinput.')
        )
    else:
        results.append(_result('static_root', PASS, 'Каталог собран.'))

    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        results.append(_result('media_root', BLOCKING, 'MEDIA_ROOT не задан.'))
    elif str(media_root) == str(static_root):
        results.append(
            _result(
                'media_isolation',
                BLOCKING,
                'MEDIA_ROOT совпадает со STATIC_ROOT: вложения нельзя раздавать публично.',
            )
        )
    else:
        results.append(
            _result('media_isolation', PASS, 'MEDIA_ROOT отделён от публичной статики.')
        )
    return results


def _demo_reset():
    if getattr(settings, 'DEMO_RESET_REQUESTED', False):
        status = BLOCKING if getattr(settings, 'IS_PRODUCTION', False) else WARNING
        return [_result('demo_reset', status, 'ENABLE_DEMO_RESET=true.')]
    return [_result('demo_reset', PASS, 'Выключен.')]


def _email():
    # The same validation `check_fresh_bootstrap` uses, so the two commands
    # can never enforce different email rules. Deliberately no SMTP connection
    # here either: a mail server being briefly unreachable is not a reason to
    # block a deployment.
    return _check_email_configuration()


def _backup():
    if getattr(settings, 'BACKUP_POLICY_ACKNOWLEDGED', False):
        return [
            _result(
                'backup_policy',
                PASS,
                'Подтверждено администратором. Это не доказательство наличия копий: '
                'требуется реальный тест восстановления.',
            )
        ]
    status = BLOCKING if getattr(settings, 'IS_PRODUCTION', False) else WARNING
    return [
        _result(
            'backup_policy',
            status,
            'BACKUP_POLICY_ACKNOWLEDGED=false. См. docs/backup_restore.md.',
        )
    ]
