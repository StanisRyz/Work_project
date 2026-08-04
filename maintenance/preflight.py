"""Source and target preflight checks for the SQLite -> PostgreSQL transfer.

Both services are strictly read-only with respect to business data:

* :func:`run_source_preflight` runs on SQLite and answers one question — *is
  this copy of the working database fit to be exported?*
* :func:`run_target_preflight` runs on PostgreSQL and answers the mirror
  question — *is this database fit to receive an import?*

Neither writes a single business row. The target check does open a
transaction to prove it may write, but always rolls it back.
"""

import json
import os
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, connection, transaction

from .database_transfer import (
    TRANSFERABLE_MODELS,
    TransferError,
    UnsafePathError,
    check_act_number_uniqueness,
    check_relational_invariants,
    describe_directory,
    directory_is_empty,
    find_lagging_act_number_sequences,
    get_applied_migration_state,
    normalize_relative_path,
    resolve_inside,
    resolve_media_source,
    safe_path_label,
)


OK = 'ok'
WARNING = 'warning'
FAILED = 'failed'


class PreflightReport:
    """Ordered list of named checks plus the derived overall status."""

    def __init__(self, kind):
        self.kind = kind
        self.checks = []
        self.summary = {}

    def add(self, name, status, details, **extra):
        entry = {'name': name, 'status': status, 'details': details}
        entry.update(extra)
        self.checks.append(entry)
        return entry

    def ok(self, name, details, **extra):
        return self.add(name, OK, details, **extra)

    def warn(self, name, details, **extra):
        return self.add(name, WARNING, details, **extra)

    def fail(self, name, details, **extra):
        return self.add(name, FAILED, details, **extra)

    @property
    def failures(self):
        return [check for check in self.checks if check['status'] == FAILED]

    @property
    def warnings(self):
        return [check for check in self.checks if check['status'] == WARNING]

    def as_dict(self):
        return {
            'kind': self.kind,
            'checked_at': datetime.now(dt_timezone.utc).isoformat(),
            'vendor': connection.vendor,
            'ok': not self.failures,
            'checks': self.checks,
            'failures': [check['name'] for check in self.failures],
            'warnings': [check['details'] for check in self.warnings],
            'summary': self.summary,
        }


# --------------------------------------------------------------------------
# Source (SQLite)
# --------------------------------------------------------------------------

def get_sqlite_database_path():
    return Path(connection.settings_dict['NAME'])


def is_default_working_database():
    """Whether the configured SQLite file is the live `BASE_DIR/db.sqlite3`."""
    default = Path(settings.BASE_DIR) / 'db.sqlite3'
    try:
        return get_sqlite_database_path().resolve() == default.resolve()
    except OSError:
        return False


def sqlite_integrity_check():
    with connection.cursor() as cursor:
        cursor.execute('PRAGMA integrity_check')
        rows = cursor.fetchall()
    return [str(row[0]) for row in rows]


def run_source_preflight(source_media_root=None, allow_default_database=False):
    """Read-only inspection of a stopped SQLite copy before exporting it."""
    report = PreflightReport('source')

    if connection.vendor != 'sqlite':
        raise TransferError(
            f'Проверка источника выполняется только на SQLite, текущий backend — '
            f'{connection.vendor}. Укажите DATABASE_ENGINE=sqlite и SQLITE_DB_PATH.'
        )
    report.ok('backend', 'Backend соответствует ожидаемому SQLite.', vendor=connection.vendor)

    # Every remaining check still runs even when the file itself looks wrong:
    # a partial report is far more useful to the operator than an early exit.
    database_path = get_sqlite_database_path()
    if not database_path.is_file():
        report.fail('database_file', f'Файл базы не найден: {safe_path_label(database_path)}.')
        report.summary['database'] = {'name': database_path.name, 'size': None}
    elif not os.access(database_path, os.R_OK):
        report.fail('database_file', f'Файл базы недоступен для чтения: {database_path.name}.')
        report.summary['database'] = {'name': database_path.name, 'size': None}
    else:
        size = database_path.stat().st_size
        report.ok(
            'database_file',
            f'Файл базы доступен: {database_path.name}, {size} байт.',
            file_name=database_path.name,
            file_size=size,
        )
        report.summary['database'] = {'name': database_path.name, 'size': size}

    if is_default_working_database():
        message = (
            'Используется рабочий файл db.sqlite3, а не отдельная копия. '
            'Остановите приложение, скопируйте базу и укажите SQLITE_DB_PATH на копию.'
        )
        if allow_default_database:
            report.warn('database_copy', message + ' Запуск разрешён флагом --allow-default-database.')
        else:
            report.fail(
                'database_copy',
                message + ' Повторите с --allow-default-database, если это осознанное решение.',
            )
    else:
        report.ok('database_copy', 'Используется отдельная копия базы, а не рабочий db.sqlite3.')

    # A damaged file can make any of the following raise instead of returning a
    # verdict, so every database-backed check is guarded and reported as a
    # failure rather than a traceback.
    _guarded(report, 'migrations', lambda: _check_migrations(report))
    _guarded(report, 'integrity_check', lambda: _check_integrity(report))
    _guarded(report, 'relations', lambda: _check_relations(report))
    _guarded(report, 'act_numbers_unique', lambda: _check_act_numbers(report))
    _guarded(report, 'act_number_sequence', lambda: _check_act_number_sequence(report))
    _guarded(report, 'attachments', lambda: _check_source_attachments(report, source_media_root))
    _guarded(report, 'inventory', lambda: _check_inventory(report))

    return report.as_dict()


def _guarded(report, name, function):
    """Run one check, turning an unreachable database into a clear failure."""
    try:
        return function()
    except DatabaseError as exc:
        report.fail(name, f'Обращение к базе завершилось ошибкой: {exc}.')
        return None


def _check_migrations(report):
    state = get_applied_migration_state()
    if state['pending']:
        report.fail(
            'migrations',
            'Не применены миграции: ' + ', '.join(state['pending']) + '.',
            pending=state['pending'],
        )
    else:
        report.ok('migrations', f'Все миграции применены ({len(state["applied"])}).')


def _check_integrity(report):
    integrity = sqlite_integrity_check()
    if integrity == ['ok']:
        report.ok('integrity_check', 'PRAGMA integrity_check — ok.')
    else:
        report.fail(
            'integrity_check',
            'PRAGMA integrity_check обнаружил повреждения: ' + '; '.join(integrity[:10]) + '.',
        )


def _check_relations(report):
    relations = check_relational_invariants()
    if relations['problems']:
        report.fail(
            'relations',
            'Нарушены реляционные инварианты: ' + ' '.join(relations['problems']),
            problems=relations['problems'],
        )
    else:
        report.ok('relations', 'Ключевые реляционные инварианты соблюдены.')


def _check_act_numbers(report):
    duplicates = check_act_number_uniqueness()
    if duplicates:
        report.fail(
            'act_numbers_unique',
            'Повторяющиеся стандартные номера актов: ' + ', '.join(duplicates) + '.',
        )
    else:
        report.ok('act_numbers_unique', 'Стандартные номера актов уникальны.')


def _check_act_number_sequence(report):
    lagging, highest = find_lagging_act_number_sequences()
    if lagging:
        details = ', '.join(
            f'{year}: счётчик {values["counter"]} < факт {values["highest"]}'
            for year, values in sorted(lagging.items())
        )
        report.fail('act_number_sequence', 'Отстают счётчики ActNumberSequence — ' + details + '.')
    else:
        report.ok(
            'act_number_sequence',
            f'ActNumberSequence не отстаёт (лет с актами — {len(highest)}).',
        )


def _check_inventory(report):
    counts = {label: apps.get_model(label)._default_manager.count() for label in TRANSFERABLE_MODELS}
    report.summary['models'] = len(TRANSFERABLE_MODELS)
    report.summary['rows'] = counts
    report.summary['total_rows'] = sum(counts.values())
    report.ok(
        'inventory',
        f'Моделей — {len(TRANSFERABLE_MODELS)}, записей — {sum(counts.values())}, '
        f'вложений — {counts.get("acts.ActAttachment", 0)}.',
    )


def _check_source_attachments(report, source_media_root):
    """Path safety, existence and declared size of every ActAttachment file."""
    try:
        media_root, media_is_default = resolve_media_source(source_media_root)
    except TransferError as exc:
        report.fail('source_media', str(exc))
        return

    stats = describe_directory(media_root)
    report.summary['source_media'] = {
        'name': media_root.name,
        'is_default_media_root': media_is_default,
        'file_count': stats['file_count'],
        'total_size': stats['total_size'],
    }
    report.ok(
        'source_media',
        f'Каталог media выбран: {media_root.name}, файлов — {stats["file_count"]}, '
        f'{stats["total_size"]} байт.',
    )

    ActAttachment = apps.get_model('acts.ActAttachment')
    unsafe = []
    absent = []
    size_mismatch = []
    empty_names = []
    checked = 0

    for attachment_pk, raw_name, declared_size in ActAttachment.objects.order_by('pk').values_list(
        'pk', 'file', 'file_size'
    ):
        if not (raw_name or '').strip():
            empty_names.append(attachment_pk)
            continue
        try:
            relative = normalize_relative_path(raw_name)
            source = resolve_inside(media_root, relative)
        except UnsafePathError as exc:
            unsafe.append(f'id={attachment_pk}: {exc}')
            continue
        if not source.is_file():
            absent.append(f'id={attachment_pk}: {relative}')
            continue
        checked += 1
        if declared_size:
            actual = source.stat().st_size
            if actual != declared_size:
                size_mismatch.append(
                    f'id={attachment_pk}: {relative} — {actual} байт вместо {declared_size}'
                )

    report.summary['attachments'] = {
        'total': ActAttachment.objects.count(),
        'files_present': checked,
        'missing': len(absent),
    }

    if unsafe:
        report.fail('attachment_paths', 'Небезопасные пути вложений: ' + '; '.join(unsafe[:10]) + '.')
    else:
        report.ok('attachment_paths', 'Все пути ActAttachment безопасны.')

    if empty_names:
        report.warn(
            'attachment_empty_paths',
            f'Записи ActAttachment без файла: {empty_names[:10]} (всего {len(empty_names)}).',
        )

    if absent:
        report.fail(
            'attachment_files',
            f'Отсутствуют файлы вложений ({len(absent)}): ' + '; '.join(absent[:10]) + '.',
            missing=absent,
        )
    else:
        report.ok('attachment_files', f'Все файлы вложений найдены ({checked}).')

    if size_mismatch:
        report.fail(
            'attachment_sizes',
            f'Размер файла не совпадает с ActAttachment.file_size ({len(size_mismatch)}): '
            + '; '.join(size_mismatch[:10]) + '.',
        )
    else:
        report.ok('attachment_sizes', 'Заполненные ActAttachment.file_size совпадают с файлами.')


# --------------------------------------------------------------------------
# Target (PostgreSQL)
# --------------------------------------------------------------------------

REQUIRED_TABLE_PRIVILEGES = ('SELECT', 'INSERT', 'UPDATE', 'DELETE')


def run_target_preflight(previous_report=None):
    """Read-only inspection of the empty PostgreSQL database before an import."""
    report = PreflightReport('target')

    if connection.vendor != 'postgresql':
        raise TransferError(
            f'Проверка целевой базы выполняется только на PostgreSQL, текущий backend — '
            f'{connection.vendor}.'
        )
    report.ok('backend', 'Backend соответствует ожидаемому PostgreSQL.', vendor=connection.vendor)

    # An unreachable or misconfigured server must produce a readable verdict,
    # never a raw traceback the operator has to decode.
    report.summary['postgresql_version'] = _guarded(
        report, 'postgresql_version', lambda: _postgresql_version(report)
    ) or ''

    _guarded(report, 'migrations', lambda: _check_migrations(report))

    if getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        report.fail(
            'email_disabled',
            'EMAIL_NOTIFICATIONS_ENABLED=true. Отключите отправку email на время переноса.',
        )
    else:
        report.ok('email_disabled', 'Email-уведомления отключены (EMAIL_NOTIFICATIONS_ENABLED=false).')

    _guarded(report, 'tables_empty', lambda: _check_target_tables_empty(report))

    media_root = Path(settings.MEDIA_ROOT)
    if directory_is_empty(media_root):
        report.ok(
            'media_root_empty',
            f'MEDIA_ROOT пуст или отсутствует: {safe_path_label(media_root)}.',
        )
    else:
        stats = describe_directory(media_root)
        report.fail(
            'media_root_empty',
            f'MEDIA_ROOT не пуст: {safe_path_label(media_root)}, файлов — {stats["file_count"]}.',
        )
    report.summary['media_root'] = safe_path_label(media_root)

    _guarded(report, 'read_write', lambda: _check_target_access(report))
    _check_previous_rehearsal(report, previous_report)

    return report.as_dict()


def _check_target_tables_empty(report):
    non_empty = []
    counts = {}
    for label in TRANSFERABLE_MODELS:
        count = apps.get_model(label)._default_manager.count()
        counts[label] = count
        if count:
            non_empty.append(f'{label}: {count}')
    report.summary['rows'] = counts
    if non_empty:
        report.fail(
            'tables_empty',
            'Переносимые таблицы не пусты: ' + ', '.join(non_empty)
            + '. Используйте prepare_empty_migration_target или очистите базу вручную.',
        )
    else:
        report.ok('tables_empty', 'Все переносимые таблицы пусты.')


def _postgresql_version(report):
    with connection.cursor() as cursor:
        cursor.execute('SELECT version()')
        raw = cursor.fetchone()[0]
    # "PostgreSQL 17.2 on x86_64-pc-linux-gnu, compiled by ..." — the first two
    # words are enough and reveal nothing about the host.
    short = ' '.join(str(raw).split()[:2])
    report.ok('postgresql_version', f'Версия PostgreSQL — {short}.', version=short)
    return short


def _check_target_access(report):
    """Prove the database is readable and writable, then roll everything back."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
                cursor.execute(
                    'CREATE TEMPORARY TABLE maintenance_target_preflight (value integer) '
                    'ON COMMIT DROP'
                )
                cursor.execute('INSERT INTO maintenance_target_preflight (value) VALUES (1)')
                cursor.execute('SELECT count(*) FROM maintenance_target_preflight')
                written = cursor.fetchone()[0]
            transaction.set_rollback(True)
    except DatabaseError as exc:
        report.fail('read_write', f'База недоступна для чтения и записи: {exc}.')
        return
    if written != 1:
        report.fail('read_write', 'Пробная запись во временную таблицу не подтвердилась.')
    else:
        report.ok('read_write', 'База доступна для чтения и записи (пробная транзакция откачена).')

    try:
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT current_user, '
                "has_database_privilege(current_user, current_database(), 'CONNECT'), "
                "has_database_privilege(current_user, current_database(), 'TEMPORARY')"
            )
            user_name, can_connect, can_temp = cursor.fetchone()
            table = apps.get_model('acts.Act')._meta.db_table
            granted = []
            for privilege in REQUIRED_TABLE_PRIVILEGES:
                cursor.execute(
                    'SELECT has_table_privilege(current_user, %s, %s)', [table, privilege]
                )
                if cursor.fetchone()[0]:
                    granted.append(privilege)
    except DatabaseError as exc:
        report.fail('privileges', f'Не удалось проверить права пользователя базы: {exc}.')
        return

    missing = [privilege for privilege in REQUIRED_TABLE_PRIVILEGES if privilege not in granted]
    if missing or not can_connect or not can_temp:
        report.fail(
            'privileges',
            f'Пользователю {user_name} не хватает прав: '
            + ', '.join(missing + ([] if can_connect else ['CONNECT']) + ([] if can_temp else ['TEMPORARY']))
            + '.',
        )
    else:
        report.ok('privileges', f'Пользователь {user_name} имеет необходимые права.')


def _check_previous_rehearsal(report, previous_report):
    """Refuse a target that still carries an unfinished previous import."""
    if not previous_report:
        report.ok('previous_import', 'Предыдущий отчёт репетиции не указан — проверка пропущена.')
        return
    path = Path(previous_report)
    if not path.is_file():
        report.ok('previous_import', 'Предыдущего отчёта репетиции нет — незавершённых импортов нет.')
        return
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        report.warn('previous_import', f'Не удалось прочитать предыдущий отчёт репетиции: {exc}.')
        return
    status = str(data.get('status', '')).lower()
    if status in {'partial', 'failed', 'running'}:
        report.fail(
            'previous_import',
            f'Предыдущая репетиция завершилась со статусом «{status}». '
            'Пересоздайте тестовую PostgreSQL и MEDIA_ROOT перед повторным импортом.',
        )
    else:
        report.ok('previous_import', f'Предыдущая репетиция завершилась со статусом «{status or "?"}».')
