"""Verify a *fresh* PostgreSQL installation is correctly prepared.

This is the first-run check: an empty database, migrated and seeded with
reference data, with no demo or synthetic content and no development-only
feature left switched on. It is **read-only** — it creates nothing, deletes
nothing and modifies nothing.

Deliberately *not* an error: the presence of real users or real acts. The
command is meant to be re-runnable after an administrator has been created and
the pilot has started; existing working data is reported as a warning so the
operator knows this is no longer a blank installation.

No usernames, act numbers, task texts or other object contents are printed.
"""

import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection


# Reference rows the workflow cannot run without.
REQUIRED_ACT_STATUS_CODES = (
    'CREATED_OTK',
    'KO_REVIEW',
    'TO_ANALYSIS',
    'OTK_REVIEW',
    'ARCHIVED',
)
REQUIRED_TASK_STATUS_CODES = ('IN_PROGRESS', 'COMPLETED')

# Markers left by the local demo and performance tooling.
SYNTHETIC_MARKER = 'PERF-SYNTHETIC'
DEMO_ADMIN_USERNAME = 'admin_user'

PASS = 'ok'
WARNING = 'warning'
BLOCKING = 'blocking'


class Command(BaseCommand):
    help = (
        'Проверяет готовность чистой установки PostgreSQL к первому запуску: '
        'backend, соединение, миграции, справочники, отсутствие '
        'демонстрационных данных и небезопасных флагов. Только чтение.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--allow-demo-admin',
            action='store_true',
            help='Не считать наличие демонстрационного администратора блокирующим.',
        )
        parser.add_argument(
            '--allow-sqlite',
            action='store_true',
            help='Разрешить запуск на SQLite (только для локальной отладки самой команды).',
        )

    def handle(self, *args, **options):
        results = run_fresh_bootstrap_checks(
            allow_demo_admin=options['allow_demo_admin'],
            allow_sqlite=options['allow_sqlite'],
        )
        blocking = [item for item in results if item['status'] == BLOCKING]
        warnings = [item for item in results if item['status'] == WARNING]

        for item in results:
            self.stdout.write(f'  [{_label(item["status"]):<8}] {item["check"]}: {item["detail"]}')

        self.stdout.write('')
        if blocking:
            self.stdout.write(
                self.style.ERROR(
                    f'Установка не готова: блокирующих проблем — {len(blocking)}, '
                    f'предупреждений — {len(warnings)}.'
                )
            )
            # A non-zero exit code so a deployment script stops here.
            raise SystemExit(1)
        if warnings:
            self.stdout.write(
                self.style.WARNING(f'Готово с предупреждениями: {len(warnings)}.')
            )
        else:
            self.stdout.write(self.style.SUCCESS('Чистая установка готова к первому запуску.'))
        return None


def _label(status):
    return {PASS: 'PASS', WARNING: 'WARNING', BLOCKING: 'BLOCKING'}[status]


def _result(check, status, detail):
    return {'check': check, 'status': status, 'detail': detail}


def run_fresh_bootstrap_checks(*, allow_demo_admin=False, allow_sqlite=False):
    """Return a list of `{check, status, detail}` — never raises, never writes."""
    results = []
    results.append(_check_backend(allow_sqlite))
    results.append(_check_connection())
    results.append(_check_migrations())
    results.extend(_check_reference_data())
    results.append(_check_act_number_sequence())
    results.extend(_check_no_demo_data(allow_demo_admin))
    results.append(_check_demo_reset_flag())
    results.append(_check_media_root())
    results.append(_check_static_root())
    results.extend(_check_service_configuration())
    return results


# -- individual checks ------------------------------------------------------

def _check_backend(allow_sqlite):
    vendor = connection.vendor
    if vendor == 'postgresql':
        return _result('backend', PASS, 'PostgreSQL.')
    if allow_sqlite:
        return _result('backend', WARNING, f'{vendor} (разрешено флагом --allow-sqlite).')
    return _result(
        'backend',
        BLOCKING,
        f'{vendor}. Первый production-запуск выполняется только на PostgreSQL.',
    )


def _check_connection():
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - the type is the useful part
        # No connection string, host or password in the message.
        return _result('connection', BLOCKING, f'Нет соединения с базой ({type(exc).__name__}).')
    return _result('connection', PASS, 'Соединение установлено.')


def _check_migrations():
    from django.db.migrations.executor import MigrationExecutor

    try:
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    except Exception as exc:  # noqa: BLE001
        return _result('migrations', BLOCKING, f'Не удалось проверить ({type(exc).__name__}).')
    if plan:
        return _result(
            'migrations', BLOCKING, f'Не применено миграций: {len(plan)}. Выполните migrate.'
        )
    return _result('migrations', PASS, 'Все миграции применены.')


def _check_reference_data():
    from references.models import ActStatus, DefectType, Operation, TaskStatus

    results = []
    for model, codes, name in (
        (ActStatus, REQUIRED_ACT_STATUS_CODES, 'act_statuses'),
        (TaskStatus, REQUIRED_TASK_STATUS_CODES, 'task_statuses'),
    ):
        try:
            present = set(model.objects.values_list('code', flat=True))
        except Exception as exc:  # noqa: BLE001
            results.append(_result(name, BLOCKING, f'Недоступно ({type(exc).__name__}).'))
            continue
        missing = [code for code in codes if code not in present]
        if missing:
            results.append(
                _result(name, BLOCKING, f'Отсутствуют коды: {", ".join(missing)}. Выполните seed_references.')
            )
        else:
            results.append(_result(name, PASS, f'Все обязательные коды присутствуют ({len(codes)}).'))

    for model, name in ((Operation, 'operations'), (DefectType, 'defect_types')):
        try:
            count = model.objects.count()
        except Exception as exc:  # noqa: BLE001
            results.append(_result(name, BLOCKING, f'Недоступно ({type(exc).__name__}).'))
            continue
        if count:
            results.append(_result(name, PASS, f'Записей: {count}.'))
        else:
            results.append(_result(name, BLOCKING, 'Справочник пуст. Выполните seed_references.'))
    return results


def _check_act_number_sequence():
    from acts.models import Act, ActNumberSequence

    try:
        rows = list(ActNumberSequence.objects.values_list('year', 'last_value'))
        act_count = Act.objects.count()
    except Exception as exc:  # noqa: BLE001
        return _result('act_number_sequence', BLOCKING, f'Недоступно ({type(exc).__name__}).')

    if not rows and not act_count:
        return _result('act_number_sequence', PASS, 'Счётчик пуст, актов нет — ожидаемо.')
    lagging = [year for year, last_value in rows if last_value < 0]
    if lagging:
        return _result('act_number_sequence', BLOCKING, 'Счётчик содержит отрицательные значения.')
    return _result(
        'act_number_sequence', PASS, f'Строк счётчика: {len(rows)}, актов: {act_count}.'
    )


def _check_no_demo_data(allow_demo_admin):
    from django.contrib.auth import get_user_model

    from acts.models import Act
    from tasks.models import Task

    results = []
    try:
        synthetic_acts = Act.objects.filter(number__startswith=SYNTHETIC_MARKER).count()
        synthetic_tasks = Task.objects.filter(task_text__startswith=SYNTHETIC_MARKER).count()
        perf_users = get_user_model().objects.filter(username__startswith='perf_user_').count()
    except Exception as exc:  # noqa: BLE001
        return [_result('synthetic_data', BLOCKING, f'Недоступно ({type(exc).__name__}).')]

    synthetic_total = synthetic_acts + synthetic_tasks + perf_users
    if synthetic_total:
        results.append(
            _result(
                'synthetic_data',
                BLOCKING,
                f'Найдены синтетические записи {SYNTHETIC_MARKER}: {synthetic_total}. '
                'Такая база не должна использоваться как рабочая.',
            )
        )
    else:
        results.append(_result('synthetic_data', PASS, 'Синтетических данных нет.'))

    demo_admin_exists = get_user_model().objects.filter(username=DEMO_ADMIN_USERNAME).exists()
    if demo_admin_exists and not allow_demo_admin:
        results.append(
            _result(
                'demo_admin',
                BLOCKING,
                'Найдена демонстрационная учётная запись администратора с известным '
                'паролем. Удалите её или подтвердите флагом --allow-demo-admin.',
            )
        )
    elif demo_admin_exists:
        results.append(_result('demo_admin', WARNING, 'Демонстрационный администратор разрешён флагом.'))
    else:
        results.append(_result('demo_admin', PASS, 'Демонстрационного администратора нет.'))

    # Real working data is not an error: the command must stay re-runnable.
    try:
        act_count = Act.objects.exclude(number__startswith=SYNTHETIC_MARKER).count()
        user_count = get_user_model().objects.count()
    except Exception:  # noqa: BLE001
        return results
    if act_count or user_count > 1:
        results.append(
            _result(
                'existing_data',
                WARNING,
                f'База уже содержит рабочие данные (актов: {act_count}, учётных записей: '
                f'{user_count}). Это не ошибка, но установка больше не является чистой.',
            )
        )
    else:
        results.append(_result('existing_data', PASS, 'Рабочих данных нет — установка чистая.'))
    return results


def _check_demo_reset_flag():
    if getattr(settings, 'DEMO_RESET_REQUESTED', False):
        return _result(
            'demo_reset',
            BLOCKING,
            'ENABLE_DEMO_RESET=true. Безвозвратное удаление всех актов не должно '
            'быть доступно в рабочей установке.',
        )
    return _result('demo_reset', PASS, 'ENABLE_DEMO_RESET выключен.')


def _check_media_root():
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if not media_root:
        return _result('media_root', BLOCKING, 'MEDIA_ROOT не задан.')
    path = Path(media_root)
    if not path.is_dir():
        return _result('media_root', BLOCKING, 'Каталог MEDIA_ROOT не существует.')
    if not os.access(path, os.W_OK):
        return _result('media_root', BLOCKING, 'Каталог MEDIA_ROOT недоступен для записи.')
    return _result('media_root', PASS, 'Каталог существует и доступен для записи.')


def _check_static_root():
    static_root = getattr(settings, 'STATIC_ROOT', None)
    if not static_root:
        return _result('static_root', BLOCKING, 'STATIC_ROOT не задан.')
    if not Path(static_root).is_dir():
        return _result(
            'static_root',
            WARNING,
            'Каталог STATIC_ROOT ещё не создан. Выполните collectstatic --noinput.',
        )
    return _result('static_root', PASS, 'Каталог собран.')


def _check_service_configuration():
    """Real-time and email must be internally consistent, without connecting."""
    results = []
    realtime_enabled = bool(getattr(settings, 'REALTIME_ENABLED', False))
    publisher = getattr(settings, 'REALTIME_PUBLISHER_BACKEND', '')
    if realtime_enabled and publisher.endswith('NoopRealtimePublisher'):
        results.append(
            _result('realtime', BLOCKING, 'REALTIME_ENABLED=true с publisher без транспорта.')
        )
    elif realtime_enabled:
        results.append(_result('realtime', PASS, 'Включён с транспортом.'))
    else:
        results.append(_result('realtime', PASS, 'Выключен — допустимая конфигурация.'))

    if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        results.append(_result('email', PASS, 'Выключен — SMTP не требуется.'))
        return results

    backend = getattr(settings, 'EMAIL_BACKEND', '')
    if backend.endswith('console.EmailBackend'):
        results.append(
            _result('email', BLOCKING, 'Уведомления включены, но backend печатает письма в консоль.')
        )
    elif getattr(settings, 'EMAIL_USE_TLS', False) and getattr(settings, 'EMAIL_USE_SSL', False):
        results.append(_result('email', BLOCKING, 'EMAIL_USE_TLS и EMAIL_USE_SSL включены одновременно.'))
    elif not str(getattr(settings, 'DEFAULT_FROM_EMAIL', '') or '').strip():
        results.append(_result('email', BLOCKING, 'DEFAULT_FROM_EMAIL пуст.'))
    else:
        results.append(_result('email', PASS, 'Конфигурация непротиворечива (без сетевой проверки).'))
    return results
