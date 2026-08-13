#!/usr/bin/env python
"""Local, non-production rehearsal of the SQLite -> PostgreSQL transfer.

Every stage is a **separate** `manage.py` subprocess on purpose: SQLite and
PostgreSQL are selected by environment variables that Django reads once at
startup, so one Python process cannot legitimately be both. Running the stages
as independent processes is what makes the rehearsal representative of the real
migration.

The script never touches the working database or the production environment:

* the SQLite source must be an explicit copy (`--source-db`);
* media is read from an explicit copy (`--source-media`);
* the target media directory is explicit (`--target-media`);
* PostgreSQL credentials are taken **only** from the environment — no password
  is ever passed on a command line or written to a report.

Usage:

    python scripts/run_postgresql_rehearsal.py \
        --source-db transfer/db-copy.sqlite3 \
        --source-media transfer/media-copy \
        --bundle transfer/bundle \
        --target-media transfer/target-media \
        --json-report transfer/rehearsal-report.json \
        --markdown-report transfer/rehearsal-report.md
"""

import argparse
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_POSTGRES_ENV = ('DB_NAME', 'DB_USER', 'DB_PASSWORD')

# Values that must never appear in captured output or in a report.
SECRET_ENV_NAMES = (
    'DB_PASSWORD',
    'SECRET_KEY',
    'EMAIL_HOST_PASSWORD',
    'PGPASSWORD',
)

REDACTED = '***'

ACCEPT_MISSING_MEDIA_PHRASE = 'ПРИНЯТЬ НЕПОЛНЫЙ ПЕРЕНОС'


class RehearsalError(Exception):
    """A refusal raised before any subprocess is started."""


# --------------------------------------------------------------------------
# Secret handling
# --------------------------------------------------------------------------

def collect_secret_values(environ):
    values = set()
    for name in SECRET_ENV_NAMES:
        value = (environ.get(name) or '').strip()
        if len(value) >= 3:
            values.add(value)
    return values


def redact(text, secrets):
    if not text:
        return ''
    cleaned = str(text)
    for secret in secrets:
        cleaned = cleaned.replace(secret, REDACTED)
    return redact_paths(cleaned)


def safe_path_label(path, keep=2):
    """Short, non-sensitive label — reports never carry full server paths."""
    parts = Path(path).parts
    if len(parts) <= keep:
        return '/'.join(parts)
    return '.../' + '/'.join(parts[-keep:])


# A drive-rooted Windows path, or a POSIX path of at least two segments. The
# POSIX branch deliberately refuses a separator preceded by a word character so
# ordinary prose such as "и/или" is left alone.
ABSOLUTE_PATH_PATTERN = re.compile(
    r'[A-Za-z]:[\\/][^\s"\'<>|,;]*'
    r'|(?<![\w/.])/(?:[\w.@+-]+/)+[\w.@+-]*'
)


def redact_paths(text):
    """Shorten every absolute path so a report never exposes server layout."""
    return ABSOLUTE_PATH_PATTERN.sub(lambda match: safe_path_label(match.group(0)), text)


# --------------------------------------------------------------------------
# Environment / inputs
# --------------------------------------------------------------------------

def directory_stats(path):
    root = Path(path)
    count = 0
    total = 0
    if root.is_dir():
        for entry in root.rglob('*'):
            if entry.is_file():
                count += 1
                total += entry.stat().st_size
    return {'file_count': count, 'total_size': total}


def git_commit_sha():
    try:
        completed = subprocess.run(
            ['git', 'rev-parse', 'HEAD'],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ''
    return completed.stdout.strip() if completed.returncode == 0 else ''


def library_versions():
    versions = {
        'python': platform.python_version(),
        'sqlite': sqlite3.sqlite_version,
        'django': '',
        'psycopg': '',
        'postgresql': '',
    }
    try:
        import django  # noqa: PLC0415 - optional, reported only

        versions['django'] = django.get_version()
    except Exception:  # noqa: BLE001
        versions['django'] = 'недоступно'
    try:
        import psycopg  # noqa: PLC0415 - optional, reported only

        versions['psycopg'] = getattr(psycopg, '__version__', 'установлен')
    except Exception:  # noqa: BLE001
        versions['psycopg'] = 'недоступно'
    return versions


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Stage execution
# --------------------------------------------------------------------------

class StageResult:
    def __init__(self, name, argv, returncode, stdout, stderr, duration):
        self.name = name
        self.argv = argv
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.duration = duration

    @property
    def ok(self):
        return self.returncode == 0


def run_manage_command(name, arguments, environ, timeout=3600):
    """Run one `manage.py` command as an isolated subprocess."""
    argv = [sys.executable, str(REPO_ROOT / 'manage.py'), *arguments]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=environ,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        return StageResult(
            name, argv, 124, exc.stdout or '', f'Превышен таймаут {timeout} с.', duration
        )
    except OSError as exc:
        duration = time.monotonic() - started
        return StageResult(name, argv, 127, '', f'Не удалось запустить команду: {exc}', duration)
    duration = time.monotonic() - started
    return StageResult(
        name, argv, completed.returncode, completed.stdout, completed.stderr, duration
    )


class Rehearsal:
    """Sequential rehearsal: the first failing stage stops everything."""

    # Stages that require the source application to be stopped; their measured
    # duration is what a real switchover window has to cover.
    DOWNTIME_STAGES = (
        'source_preflight',
        'export_bundle',
        'validate_bundle',
        'import_dry_run',
        'import_bundle',
        'verify_bundle',
    )

    def __init__(self, options, runner=run_manage_command, environ=None):
        self.options = options
        self.runner = runner
        self.environ = dict(environ if environ is not None else os.environ)
        self.secrets = collect_secret_values(self.environ)
        self.stages = []
        self.warnings = []
        self.blocking = []
        self.artifacts = {}
        self.started_at = datetime.now(dt_timezone.utc)
        self.status = 'running'
        self.work_dir = Path(options.json_report).resolve().parent

    # -- environments ----------------------------------------------------

    def sqlite_env(self):
        env = dict(self.environ)
        env['DATABASE_ENGINE'] = 'sqlite'
        env['SQLITE_DB_PATH'] = str(Path(self.options.source_db).resolve())
        env['EMAIL_NOTIFICATIONS_ENABLED'] = 'false'
        env['PYTHONIOENCODING'] = 'utf-8'
        return env

    def postgres_env(self):
        env = dict(self.environ)
        env['DATABASE_ENGINE'] = 'postgresql'
        env['MEDIA_ROOT_PATH'] = str(Path(self.options.target_media).resolve())
        env['EMAIL_NOTIFICATIONS_ENABLED'] = 'false'
        env['PYTHONIOENCODING'] = 'utf-8'
        env.pop('SQLITE_DB_PATH', None)
        return env

    # -- validation ------------------------------------------------------

    def validate_inputs(self):
        source_db = Path(self.options.source_db)
        if not source_db.is_file():
            raise RehearsalError(f'Копия SQLite не найдена: {safe_path_label(source_db)}.')
        default_db = REPO_ROOT / 'db.sqlite3'
        try:
            is_default = source_db.resolve() == default_db.resolve()
        except OSError:
            is_default = False
        if is_default and not self.options.allow_default_source_db:
            raise RehearsalError(
                'Указан рабочий db.sqlite3. Остановите приложение, сделайте копию и '
                'укажите её в --source-db, либо повторите с --allow-default-source-db.'
            )
        if is_default:
            self.warnings.append(
                'Репетиция выполняется на рабочем db.sqlite3 (разрешено --allow-default-source-db).'
            )

        source_media = Path(self.options.source_media)
        if not source_media.is_dir():
            raise RehearsalError(
                f'Копия каталога media не найдена: {safe_path_label(source_media)}.'
            )

        bundle = Path(self.options.bundle)
        if bundle.exists() and any(bundle.iterdir()):
            raise RehearsalError(
                f'Каталог пакета {safe_path_label(bundle)} не пуст. Укажите новый каталог.'
            )

        target_media = Path(self.options.target_media)
        if target_media.exists() and any(target_media.iterdir()):
            raise RehearsalError(
                f'Каталог целевого media {safe_path_label(target_media)} не пуст. '
                'Очистите его или укажите другой.'
            )

        missing = [name for name in REQUIRED_POSTGRES_ENV if not (self.environ.get(name) or '').strip()]
        if missing:
            raise RehearsalError(
                'Не заданы переменные окружения PostgreSQL: ' + ', '.join(missing)
                + '. Пароль передаётся только через окружение, не аргументом командной строки.'
            )

    # -- stages ----------------------------------------------------------

    def build_stages(self):
        reports = self.work_dir
        self.artifacts = {
            'source_preflight': reports / 'rehearsal-source-preflight.json',
            'bundle_validation': reports / 'rehearsal-bundle-validation.json',
            'target_preflight': reports / 'rehearsal-target-preflight.json',
            'import_dry_run': reports / 'rehearsal-import-dry-run.json',
            'import_bundle': reports / 'rehearsal-import.json',
            'verify_bundle': reports / 'rehearsal-verification.json',
            'smoke_checks': reports / 'rehearsal-smoke-checks.json',
        }

        source_preflight = [
            'check_migration_source',
            '--source-media-root',
            str(Path(self.options.source_media).resolve()),
            '--json-report',
            str(self.artifacts['source_preflight']),
        ]
        if self.options.allow_default_source_db:
            source_preflight.append('--allow-default-database')

        export = [
            'export_migration_bundle',
            '--output',
            str(Path(self.options.bundle).resolve()),
            '--source-media-root',
            str(Path(self.options.source_media).resolve()),
        ]
        if self.options.diagnostic_missing_media:
            export.append('--allow-missing-media')

        validate = [
            'verify_migration_bundle',
            '--input',
            str(Path(self.options.bundle).resolve()),
            '--validate-only',
            '--report',
            str(self.artifacts['bundle_validation']),
        ]

        target_preflight = [
            'check_migration_target',
            '--json-report',
            str(self.artifacts['target_preflight']),
        ]
        if Path(self.options.json_report).is_file():
            target_preflight += ['--previous-report', str(Path(self.options.json_report).resolve())]

        dry_run = [
            'import_migration_bundle',
            '--input',
            str(Path(self.options.bundle).resolve()),
            '--dry-run',
            '--json-report',
            str(self.artifacts['import_dry_run']),
        ]
        real_import = [
            'import_migration_bundle',
            '--input',
            str(Path(self.options.bundle).resolve()),
            '--json-report',
            str(self.artifacts['import_bundle']),
        ]
        if self.options.diagnostic_missing_media:
            dry_run += ['--accept-missing-media']
            real_import += ['--accept-missing-media', '--confirmation', ACCEPT_MISSING_MEDIA_PHRASE]

        verify = [
            'verify_migration_bundle',
            '--input',
            str(Path(self.options.bundle).resolve()),
            '--report',
            str(self.artifacts['verify_bundle']),
        ]
        if self.options.diagnostic_missing_media:
            verify.append('--allow-missing-media')

        smoke = [
            'run_postgresql_smoke_checks',
            '--json-report',
            str(self.artifacts['smoke_checks']),
        ]

        return [
            ('source_preflight', source_preflight, 'sqlite',
             'Повторите после устранения замечаний проверки источника; пакет не собирался.'),
            ('export_bundle', export, 'sqlite',
             'Удалите неполный каталог пакета и повторите экспорт из копии SQLite.'),
            ('validate_bundle', validate, 'sqlite',
             'Пакет повреждён или собран другой версией инструментов — пересоберите его.'),
            ('target_preflight', target_preflight, 'postgresql',
             'Пересоздайте пустую тестовую PostgreSQL и очистите целевой каталог media.'),
            ('import_dry_run', dry_run, 'postgresql',
             'Целевая база не изменена. Устраните причину и повторите dry-run.'),
            ('import_bundle', real_import, 'postgresql',
             'Пересоздайте тестовую PostgreSQL и целевой media, затем повторите импорт '
             'из того же пакета.'),
            ('verify_bundle', verify, 'postgresql',
             'Не переключайте приложение. Разберите список расхождений и повторите перенос '
             'с нуля в пересозданную базу.'),
            ('smoke_checks', smoke, 'postgresql',
             'Целевая база непригодна: разберите отказавшие проверки до повторной репетиции.'),
        ]

    def execute(self):
        try:
            self.validate_inputs()
        except RehearsalError as exc:
            self.status = 'failed'
            self.blocking.append(str(exc))
            self.stages.append(
                {
                    'name': 'preconditions',
                    'status': 'failed',
                    'command': '',
                    'duration_seconds': 0.0,
                    'error': str(exc),
                    'recommended_action': 'Исправьте входные параметры и запустите репетицию заново.',
                }
            )
            return 2

        Path(self.options.target_media).mkdir(parents=True, exist_ok=True)

        for name, arguments, backend, recovery in self.build_stages():
            env = self.sqlite_env() if backend == 'sqlite' else self.postgres_env()
            result = self.runner(name, arguments, env)
            entry = {
                'name': name,
                'backend': backend,
                'status': 'ok' if result.ok else 'failed',
                'command': redact('manage.py ' + ' '.join(arguments), self.secrets),
                'duration_seconds': round(result.duration, 3),
                'returncode': result.returncode,
                'output_tail': self._tail(result.stdout),
            }
            if not result.ok:
                entry['error'] = self._tail(result.stderr or result.stdout, lines=25)
                entry['recommended_action'] = recovery
                self.stages.append(entry)
                self.status = 'failed'
                self.blocking.append(
                    f'Этап «{name}» завершился с кодом {result.returncode}. {recovery}'
                )
                self.blocking.append(
                    'Целевая PostgreSQL не считается пригодной. Копии SQLite/media и '
                    'миграционный пакет сохранены для разбора.'
                )
                return 1
            self.stages.append(entry)

        self.status = 'ok'
        return 0

    def _tail(self, text, lines=15):
        cleaned = redact(text, self.secrets).strip()
        if not cleaned:
            return ''
        parts = cleaned.splitlines()
        return '\n'.join(parts[-lines:])

    # -- reporting -------------------------------------------------------

    def build_report(self, exit_code):
        source_db = Path(self.options.source_db)
        source_media = Path(self.options.source_media)
        media_stats = directory_stats(source_media)
        versions = library_versions()

        source_preflight = read_json(self.artifacts.get('source_preflight', '')) or {}
        bundle_validation = read_json(self.artifacts.get('bundle_validation', '')) or {}
        target_preflight = read_json(self.artifacts.get('target_preflight', '')) or {}
        import_report = read_json(self.artifacts.get('import_bundle', '')) or {}
        verification = read_json(self.artifacts.get('verify_bundle', '')) or {}
        smoke = read_json(self.artifacts.get('smoke_checks', '')) or {}

        versions['postgresql'] = (target_preflight.get('summary') or {}).get('postgresql_version', '')

        manifest = read_json(Path(self.options.bundle) / 'manifest.json') or {}
        model_hashes = {
            label: entry.get('hash')
            for label, entry in (manifest.get('models') or {}).items()
        }
        model_counts = {
            label: entry.get('count')
            for label, entry in (manifest.get('models') or {}).items()
        }

        missing_media = list(import_report.get('missing_media') or verification.get('missing_media') or [])
        if missing_media:
            self.warnings.append(f'Отсутствующих файлов вложений — {len(missing_media)}.')
            self.blocking.append(
                'Перенос неполный: часть файлов вложений отсутствует. '
                'Production-переезд с таким пакетом недопустим.'
            )
        if import_report.get('status') == 'partial':
            self.blocking.append(
                'Импорт завершился частично: media не активирована полностью.'
            )
        if verification and verification.get('ok') is False:
            self.blocking.append('Итоговая сверка нашла расхождения.')
        if smoke and smoke.get('ok') is False:
            self.blocking.append('Smoke-проверки не пройдены.')

        for entry in (source_preflight.get('warnings') or []):
            self.warnings.append(f'source preflight: {entry}')
        for entry in (target_preflight.get('warnings') or []):
            self.warnings.append(f'target preflight: {entry}')
        for entry in (bundle_validation.get('warnings') or []):
            self.warnings.append(f'bundle: {entry}')

        measured = sum(
            stage['duration_seconds']
            for stage in self.stages
            if stage['name'] in self.DOWNTIME_STAGES and stage['status'] == 'ok'
        )
        # A real switchover also needs stopping the app, copying the database and
        # media, and the manual checklist. The measured time is the floor, not
        # the plan: it is padded by 50% plus a fixed 10-minute manual allowance.
        estimated_window = measured * 1.5 + 600

        overall = 'ok' if exit_code == 0 and not self.blocking else (
            'partial' if import_report.get('status') == 'partial' else 'failed'
        )
        self.status = overall

        return {
            'schema': 'postgresql-rehearsal-report/1',
            'status': overall,
            'exit_code': exit_code,
            'started_at': self.started_at.isoformat(),
            'finished_at': datetime.now(dt_timezone.utc).isoformat(),
            'production_switched': False,
            'environment': {
                'os': f'{platform.system()} {platform.release()}',
                'platform': platform.platform(),
                'git_commit': git_commit_sha(),
                'versions': versions,
            },
            'source': {
                'sqlite_file': source_db.name,
                'sqlite_size_bytes': source_db.stat().st_size if source_db.is_file() else 0,
                'media_directory': source_media.name,
                'media_file_count': media_stats['file_count'],
                'media_total_size_bytes': media_stats['total_size'],
                'is_default_working_database': bool(self.options.allow_default_source_db),
            },
            'bundle': {
                'directory': safe_path_label(self.options.bundle),
                'format_version': manifest.get('bundle_format_version'),
                'complete': manifest.get('complete'),
                'model_count': len(model_counts),
                'record_count': bundle_validation.get('record_count'),
                'media_count': bundle_validation.get('media_count'),
                'attachment_count': model_counts.get('acts.ActAttachment'),
                'data_sha256': (manifest.get('data_file') or {}).get('sha256'),
                'counts': model_counts,
                'hashes': model_hashes,
            },
            'stages': self.stages,
            'source_preflight': _summarise_preflight(source_preflight),
            'target_preflight': _summarise_preflight(target_preflight),
            'import': {
                'status': import_report.get('status'),
                'loaded': import_report.get('loaded'),
                'sequence_reset': import_report.get('sequences'),
                'media_copied': import_report.get('media_copied'),
                'recovery': import_report.get('recovery') or [],
            },
            'verification': {
                'ok': verification.get('ok'),
                'complete_transfer': verification.get('complete_transfer'),
                'models_matched': sum(
                    1 for entry in (verification.get('models') or {}).values()
                    if entry.get('matches')
                ),
                'models_total': len(verification.get('models') or {}),
                'media_checked': (verification.get('media') or {}).get('checked'),
                'media_expected': (verification.get('media') or {}).get('expected'),
                'relational_problems': (verification.get('relations') or {}).get('problems', []),
                'differences': verification.get('differences', []),
            },
            'smoke_checks': {
                'ok': smoke.get('ok'),
                'duration_seconds': smoke.get('duration_seconds'),
                'read': _summarise_suite(smoke.get('read')),
                'write': _summarise_suite(smoke.get('write')),
            },
            'missing_media': missing_media,
            'warnings': self.warnings,
            'blocking_issues': self.blocking,
            'downtime_estimate': {
                'measured_seconds': round(measured, 1),
                'estimated_window_seconds': round(estimated_window, 1),
                'estimated_window_minutes': round(estimated_window / 60, 1),
                'basis': (
                    'Замеренная длительность этапов, требующих остановки приложения, '
                    'плюс 50% запаса и 10 минут на ручные действия и checklist.'
                ),
            },
        }


def _summarise_preflight(report):
    if not report:
        return {'ok': None, 'checks': [], 'failures': [], 'summary': {}}
    return {
        'ok': report.get('ok'),
        'checks': [
            {'name': check.get('name'), 'status': check.get('status'), 'details': check.get('details')}
            for check in report.get('checks', [])
        ],
        'failures': report.get('failures', []),
        'summary': report.get('summary', {}),
    }


def _summarise_suite(suite):
    if not suite:
        return {'ok': None, 'checks': [], 'failures': []}
    return {
        'ok': suite.get('ok'),
        'checks': [
            {'name': check.get('name'), 'status': check.get('status'), 'details': check.get('details')}
            for check in suite.get('checks', [])
        ],
        'failures': suite.get('failures', []),
    }


# --------------------------------------------------------------------------
# Report writers
# --------------------------------------------------------------------------

STATUS_LABELS = {
    'ok': 'УСПЕШНО',
    'partial': 'ЧАСТИЧНО',
    'failed': 'ОШИБКА',
    'running': 'ВЫПОЛНЯЕТСЯ',
}


def write_json_report(path, report):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False), encoding='utf-8'
    )
    return target


def render_markdown(report):
    lines = []
    add = lines.append

    add('# Отчёт репетиции переноса SQLite → PostgreSQL')
    add('')
    add(f'**Общий статус:** {STATUS_LABELS.get(report["status"], report["status"])}')
    add('')
    add('**Рабочая система на PostgreSQL не переключена.** Репетиция выполнена на копиях.')
    add('')

    add('## Запуск и окружение')
    add('')
    environment = report['environment']
    versions = environment['versions']
    add('| Параметр | Значение |')
    add('| --- | --- |')
    add(f'| Начало | {report["started_at"]} |')
    add(f'| Завершение | {report["finished_at"]} |')
    add(f'| ОС | {environment["os"]} |')
    add(f'| Git commit | {environment["git_commit"] or "недоступен"} |')
    add(f'| Python | {versions["python"]} |')
    add(f'| Django | {versions["django"]} |')
    add(f'| Psycopg | {versions["psycopg"]} |')
    add(f'| SQLite | {versions["sqlite"]} |')
    add(f'| PostgreSQL | {versions["postgresql"] or "недоступна"} |')
    add('')

    source = report['source']
    add('## Источник')
    add('')
    add('| Параметр | Значение |')
    add('| --- | --- |')
    add(f'| Файл SQLite | {source["sqlite_file"]} |')
    add(f'| Размер SQLite | {source["sqlite_size_bytes"]} байт |')
    add(f'| Каталог media | {source["media_directory"]} |')
    add(f'| Файлов media | {source["media_file_count"]} |')
    add(f'| Объём media | {source["media_total_size_bytes"]} байт |')
    add('')

    bundle = report['bundle']
    add('## Пакет')
    add('')
    add('| Параметр | Значение |')
    add('| --- | --- |')
    add(f'| Версия формата | {bundle["format_version"]} |')
    add(f'| Полный пакет | {"да" if bundle["complete"] else "нет"} |')
    add(f'| Моделей | {bundle["model_count"]} |')
    add(f'| Записей | {bundle["record_count"]} |')
    add(f'| Файлов media | {bundle["media_count"]} |')
    add(f'| Вложений (ActAttachment) | {bundle["attachment_count"]} |')
    add(f'| SHA-256 data.json | {bundle["data_sha256"]} |')
    add('')

    add('## Этапы')
    add('')
    add('| Этап | Backend | Статус | Длительность, с |')
    add('| --- | --- | --- | --- |')
    for stage in report['stages']:
        add(
            f'| {stage["name"]} | {stage.get("backend", "-")} | '
            f'{stage["status"]} | {stage["duration_seconds"]} |'
        )
    add('')
    for stage in report['stages']:
        if stage['status'] == 'failed':
            add(f'### Ошибка на этапе `{stage["name"]}`')
            add('')
            add(f'- Команда: `{stage.get("command", "")}`')
            add(f'- Код возврата: {stage.get("returncode")}')
            add(f'- Длительность: {stage["duration_seconds"]} с')
            add('- Сообщение:')
            add('')
            add('```')
            add(stage.get('error', ''))
            add('```')
            add('')
            add(f'- Рекомендуемое действие: {stage.get("recommended_action", "")}')
            add('')

    add('## Проверка источника')
    add('')
    _render_checks(add, report['source_preflight'])

    add('## Проверка целевой базы')
    add('')
    _render_checks(add, report['target_preflight'])

    imported = report['import']
    add('## Импорт')
    add('')
    add(f'- Статус: {imported["status"] or "не выполнялся"}')
    add(f'- Загружено записей: {imported["loaded"]}')
    add(f'- Скопировано файлов media: {imported["media_copied"]}')
    sequences = imported['sequence_reset'] or {}
    add(
        f'- Сброс последовательностей: моделей — {len(sequences.get("models") or [])}, '
        f'инструкций — {sequences.get("statements")}'
    )
    for step in imported['recovery']:
        add(f'- Восстановление: {step}')
    add('')

    verification = report['verification']
    add('## Итоговая сверка')
    add('')
    add(f'- Результат: {verification["ok"]}')
    add(f'- Полный перенос: {verification["complete_transfer"]}')
    add(f'- Совпало моделей: {verification["models_matched"]}/{verification["models_total"]}')
    add(f'- Проверено файлов media: {verification["media_checked"]}/{verification["media_expected"]}')
    if verification['relational_problems']:
        add('- Проблемы связей:')
        for problem in verification['relational_problems']:
            add(f'  - {problem}')
    else:
        add('- Проблемы связей: не обнаружены')
    if verification['differences']:
        add('- Расхождения:')
        for difference in verification['differences']:
            add(f'  - {difference}')
    add('')

    add('## Smoke-проверки')
    add('')
    smoke = report['smoke_checks']
    add(f'- Результат: {smoke["ok"]}')
    add(f'- Длительность: {smoke["duration_seconds"]} с')
    add('')
    add('### Read-only')
    add('')
    _render_checks(add, smoke['read'])
    add('### Запись с откатом')
    add('')
    _render_checks(add, smoke['write'])

    add('## Отсутствующие файлы вложений')
    add('')
    if report['missing_media']:
        for entry in report['missing_media']:
            add(f'- {entry}')
    else:
        add('- отсутствующих файлов нет')
    add('')

    add('## Предупреждения')
    add('')
    if report['warnings']:
        for warning in report['warnings']:
            add(f'- {warning}')
    else:
        add('- предупреждений нет')
    add('')

    add('## Оценка окна простоя')
    add('')
    downtime = report['downtime_estimate']
    add(f'- Замерено на этапах остановки: {downtime["measured_seconds"]} с')
    add(
        f'- Оценка минимального окна: {downtime["estimated_window_seconds"]} с '
        f'(~{downtime["estimated_window_minutes"]} мин)'
    )
    add(f'- Основание: {downtime["basis"]}')
    add('')

    add('## Блокирующие проблемы для production-переезда')
    add('')
    if report['blocking_issues']:
        for issue in report['blocking_issues']:
            add(f'- {issue}')
    else:
        add('- блокирующих проблем не обнаружено')
    add('')
    add('Ручной checklist из `docs/archive/postgresql_rehearsal.md` обязателен независимо от этого отчёта.')
    add('')

    return '\n'.join(lines)


def _render_checks(add, section):
    checks = (section or {}).get('checks') or []
    if not checks:
        add('- проверки не выполнялись')
        add('')
        return
    add('| Проверка | Статус | Детали |')
    add('| --- | --- | --- |')
    for check in checks:
        details = str(check.get('details', '')).replace('|', '\\|').replace('\n', ' ')
        add(f'| {check.get("name")} | {check.get("status")} | {details} |')
    add('')


def write_markdown_report(path, report):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(report), encoding='utf-8')
    return target


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            'Полная локальная репетиция переноса SQLite → PostgreSQL. '
            'Рабочую базу и production не переключает.'
        )
    )
    parser.add_argument('--source-db', required=True, help='Копия db.sqlite3 (не рабочий файл).')
    parser.add_argument('--source-media', required=True, help='Копия каталога media.')
    parser.add_argument('--bundle', required=True, help='Каталог для миграционного пакета.')
    parser.add_argument('--target-media', required=True, help='Каталог MEDIA_ROOT тестовой PostgreSQL.')
    parser.add_argument('--json-report', required=True, help='Путь для JSON-отчёта репетиции.')
    parser.add_argument('--markdown-report', required=True, help='Путь для Markdown-отчёта.')
    parser.add_argument(
        '--allow-default-source-db',
        action='store_true',
        help='Разрешить репетицию на рабочем db.sqlite3 (по умолчанию запрещено).',
    )
    parser.add_argument(
        '--diagnostic-missing-media',
        action='store_true',
        help=(
            'Диагностический режим: собрать и принять неполный пакет с отсутствующими '
            'файлами вложений. Перенос считается неполным.'
        ),
    )
    return parser


def main(argv=None, runner=run_manage_command, environ=None, stream=None):
    options = build_parser().parse_args(argv)
    rehearsal = Rehearsal(options, runner=runner, environ=environ)
    output = stream if stream is not None else sys.stdout

    exit_code = rehearsal.execute()
    report = rehearsal.build_report(exit_code)
    if report['status'] != 'ok' and exit_code == 0:
        exit_code = 1

    json_path = write_json_report(options.json_report, report)
    markdown_path = write_markdown_report(options.markdown_report, report)

    def emit(line):
        print(line, file=output)

    emit(f'JSON-отчёт: {json_path}')
    emit(f'Markdown-отчёт: {markdown_path}')
    emit(f'Общий статус: {STATUS_LABELS.get(report["status"], report["status"])}')
    for issue in report['blocking_issues']:
        emit(f'  Блокирующая проблема: {issue}')
    if report['status'] == 'ok':
        emit(
            'Репетиция пройдена. Копии SQLite/media и пакет сохранены; '
            'production-переезд не выполнялся.'
        )
    else:
        emit(
            'Репетиция не пройдена. Копии SQLite/media и миграционный пакет сохранены '
            'для разбора; целевая PostgreSQL не считается пригодной.'
        )
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
