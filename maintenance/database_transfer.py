"""Services for preparing a SQLite -> PostgreSQL data transfer.

The three management commands in this app are thin wrappers around the
functions here: they parse arguments, print output and translate
:class:`TransferError` into ``CommandError``.

Nothing in this module migrates the live working database. It produces and
consumes a *migration bundle* — a self-contained directory that can be
exported from a stopped copy of SQLite, checked, imported into an empty
PostgreSQL database, and finally verified.
"""

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone as dt_timezone
from pathlib import Path, PurePosixPath

import django
from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.core.management.color import no_style
from django.db import connection, models, transaction
from django.db.migrations.executor import MigrationExecutor


BUNDLE_FORMAT_VERSION = 1
MANIFEST_NAME = 'manifest.json'
DATA_NAME = 'data.json'
MEDIA_DIR_NAME = 'media'
VERIFICATION_REPORT_NAME = 'verification-report.json'

# Ordered so that every model appears after the models it points at. The same
# order drives serialization, the emptiness check and verification.
TRANSFERABLE_MODELS = (
    'auth.Group',
    'auth.User',
    'accounts.Department',
    'accounts.UserProfile',
    'references.Operation',
    'references.DefectType',
    'references.ActStatus',
    'references.TaskStatus',
    'references.Priority',
    'acts.ActNumberSequence',
    'acts.Act',
    'acts.ActDefect',
    'acts.ActRootAnalysis',
    'acts.ActCorrectiveAction',
    'acts.ActCorrectiveActionAssignee',
    'acts.ActHistoryEvent',
    'acts.ActComment',
    'acts.ActAttachment',
    'tasks.Task',
    'tasks.TaskAssignee',
    'notifications.Notification',
    'notifications.NotificationDelivery',
)

# Rebuilt by `migrate` / `post_migrate` on the target database, or intentionally
# not carried over. References to auth.Permission are exported as Django
# natural keys instead, so they resolve against the target's own rows.
EXCLUDED_MODELS = (
    'contenttypes.ContentType',
    'auth.Permission',
    'sessions.Session',
    'admin.LogEntry',
)

# Rows these data migrations create on any freshly migrated database. The
# bundle carries its own copies, so the target must be cleared of them by the
# operator before importing; this module never deletes rows itself.
MIGRATION_SEEDED_MODELS = (
    'references.ActStatus',
    'references.TaskStatus',
)

ACT_NUMBER_PATTERN = re.compile(r'^АОК-(\d{4})-(\d+)$')

CHUNK_SIZE = 1024 * 1024


class TransferError(Exception):
    """Any refusal or failure the operator needs to see and act on."""


class UnsafePathError(TransferError):
    pass


# --------------------------------------------------------------------------
# Paths and hashing
# --------------------------------------------------------------------------

def normalize_relative_path(raw_path):
    """Return a safe, normalized POSIX-style relative path.

    Rejects absolute paths, drive letters and any component that escapes the
    root, so a crafted bundle cannot write outside its destination.
    """
    text = str(raw_path or '').strip().replace('\\', '/')
    if not text:
        raise UnsafePathError('Пустой путь к файлу вложения.')
    if text.startswith('/') or re.match(r'^[A-Za-z]:', text):
        raise UnsafePathError(f'Абсолютный путь недопустим: {raw_path!r}.')
    parts = [part for part in PurePosixPath(text).parts if part not in ('.', '')]
    if any(part == '..' for part in parts):
        raise UnsafePathError(f'Путь выходит за пределы каталога: {raw_path!r}.')
    if not parts:
        raise UnsafePathError(f'Некорректный путь: {raw_path!r}.')
    return '/'.join(parts)


def resolve_inside(root, relative_path):
    """Resolve `relative_path` under `root`, refusing anything that escapes."""
    safe_relative = normalize_relative_path(relative_path)
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / safe_relative).resolve()
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError(
            f'Путь {relative_path!r} выходит за пределы каталога {root_resolved}.'
        ) from exc
    return candidate


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def directory_is_empty(path):
    directory = Path(path)
    if not directory.exists():
        return True
    if not directory.is_dir():
        return False
    return not any(directory.iterdir())


# --------------------------------------------------------------------------
# Model inspection
# --------------------------------------------------------------------------

def get_transferable_models():
    return [apps.get_model(label) for label in TRANSFERABLE_MODELS]


def serialize_model(model):
    """Serialize one model deterministically and return the JSON text.

    Natural foreign keys keep references to `auth.Permission` (and any other
    excluded natural-key model) resolvable on the target, while primary keys
    of transferred rows are preserved as-is.
    """
    queryset = model._default_manager.all().order_by('pk')
    return serializers.serialize(
        'json',
        queryset,
        use_natural_foreign_keys=True,
        use_natural_primary_keys=False,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    )


def collect_model_stats():
    """Return {label: {count, max_pk, hash}} plus the combined object list."""
    stats = {}
    combined = []
    for label in TRANSFERABLE_MODELS:
        model = apps.get_model(label)
        payload = serialize_model(model)
        records = json.loads(payload)
        aggregate = model._default_manager.aggregate(value=models.Max('pk'))
        max_pk = aggregate['value']
        stats[label] = {
            'count': len(records),
            'max_pk': max_pk if isinstance(max_pk, int) else None,
            'hash': text_sha256(payload),
        }
        combined.extend(records)
    return stats, combined


def find_non_empty_models():
    """Return labels of transferable models that already contain rows."""
    non_empty = []
    for label in TRANSFERABLE_MODELS:
        model = apps.get_model(label)
        if model._default_manager.exists():
            non_empty.append(label)
    return non_empty


def get_applied_migration_state():
    executor = MigrationExecutor(connection)
    applied = sorted(f'{app_label}.{name}' for app_label, name in executor.loader.applied_migrations)
    targets = executor.loader.graph.leaf_nodes()
    pending = sorted(
        f'{migration.app_label}.{migration.name}'
        for migration, _backwards in executor.migration_plan(targets)
    )
    return {'applied': applied, 'pending': pending}


def require_all_migrations_applied():
    state = get_applied_migration_state()
    if state['pending']:
        raise TransferError(
            'Не все миграции применены: ' + ', '.join(state['pending'])
            + '. Выполните python manage.py migrate и повторите.'
        )
    return state


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------

def export_bundle(output_dir, allow_missing_media=False):
    """Build a migration bundle from the current (SQLite) database."""
    if connection.vendor != 'sqlite':
        raise TransferError(
            f'Экспорт разрешён только на SQLite, текущий backend — {connection.vendor}.'
        )
    output_path = Path(output_dir)
    if not directory_is_empty(output_path):
        raise TransferError(
            f'Каталог {output_path} не пуст. Укажите новый или пустой каталог.'
        )
    migration_state = require_all_migrations_applied()

    staging_parent = tempfile.mkdtemp(prefix='migration-bundle-export-')
    staging = Path(staging_parent) / 'bundle'
    try:
        staging.mkdir(parents=True)
        (staging / MEDIA_DIR_NAME).mkdir()

        stats, combined = collect_model_stats()
        data_text = json.dumps(combined, indent=2, sort_keys=True, ensure_ascii=False)
        data_path = staging / DATA_NAME
        data_path.write_text(data_text, encoding='utf-8')

        media_files, missing_media, warnings = _copy_attachment_files(
            staging / MEDIA_DIR_NAME, allow_missing_media
        )

        manifest = {
            'bundle_format_version': BUNDLE_FORMAT_VERSION,
            'created_at': datetime.now(dt_timezone.utc).isoformat(),
            'source_vendor': connection.vendor,
            'python_version': sys.version.split()[0],
            'django_version': django.get_version(),
            'migration_state': migration_state,
            'data_file': {
                'name': DATA_NAME,
                'sha256': text_sha256(data_text),
                'size': data_path.stat().st_size,
            },
            'models': stats,
            'model_order': list(TRANSFERABLE_MODELS),
            'excluded_models': list(EXCLUDED_MODELS),
            'media_files': media_files,
            'missing_media': missing_media,
            'warnings': warnings,
        }
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
        (staging / MANIFEST_NAME).write_text(manifest_text, encoding='utf-8')

        # Re-read what was just written before publishing anything.
        validate_bundle(staging)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.rmdir()  # only reachable when it exists and is empty
        shutil.move(str(staging), str(output_path))
    except Exception:
        shutil.rmtree(staging_parent, ignore_errors=True)
        raise
    else:
        shutil.rmtree(staging_parent, ignore_errors=True)
    return manifest


def _copy_attachment_files(media_target, allow_missing_media):
    """Copy every file referenced by ActAttachment into the bundle."""
    ActAttachment = apps.get_model('acts.ActAttachment')
    media_root = Path(settings.MEDIA_ROOT).resolve()
    media_files = []
    missing = []
    warnings = []
    seen = set()

    queryset = ActAttachment.objects.exclude(file='').order_by('pk')
    for attachment_pk, raw_name in queryset.values_list('pk', 'file'):
        relative = normalize_relative_path(raw_name)
        if relative in seen:
            warnings.append(
                f'Файл {relative} указан более чем одной записью ActAttachment; скопирован один раз.'
            )
            continue
        seen.add(relative)
        source = resolve_inside(media_root, relative)
        if not source.exists() or not source.is_file():
            entry = {'attachment_id': attachment_pk, 'path': relative, 'reason': 'файл не найден'}
            if not allow_missing_media:
                raise TransferError(
                    f'Файл вложения отсутствует: {relative} (ActAttachment id={attachment_pk}). '
                    'Восстановите файл или запустите экспорт с --allow-missing-media.'
                )
            missing.append(entry)
            continue
        try:
            destination = resolve_inside(media_target, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            media_files.append(
                {
                    'path': relative,
                    'size': destination.stat().st_size,
                    'sha256': file_sha256(destination),
                }
            )
        except OSError as exc:
            raise TransferError(f'Не удалось прочитать файл {relative}: {exc}.') from exc

    media_files.sort(key=lambda item: item['path'])
    missing.sort(key=lambda item: (item['path'], item['attachment_id']))
    return media_files, missing, warnings


# --------------------------------------------------------------------------
# Bundle validation
# --------------------------------------------------------------------------

def load_manifest(bundle_dir):
    manifest_path = Path(bundle_dir) / MANIFEST_NAME
    if not manifest_path.is_file():
        raise TransferError(f'В пакете нет {MANIFEST_NAME}: {manifest_path}.')
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        raise TransferError(f'{MANIFEST_NAME} повреждён: {exc}.') from exc
    if not isinstance(manifest, dict):
        raise TransferError(f'{MANIFEST_NAME} должен содержать объект JSON.')
    return manifest


def validate_bundle(bundle_dir, check_migration_state=False):
    """Fully validate a bundle on disk. Raises TransferError on any problem."""
    bundle_path = Path(bundle_dir)
    if not bundle_path.is_dir():
        raise TransferError(f'Каталог пакета не найден: {bundle_path}.')

    manifest = load_manifest(bundle_path)

    version = manifest.get('bundle_format_version')
    if version != BUNDLE_FORMAT_VERSION:
        raise TransferError(
            f'Неподдерживаемая версия формата пакета: {version!r}. '
            f'Поддерживается только {BUNDLE_FORMAT_VERSION}.'
        )

    for key in ('created_at', 'source_vendor', 'data_file', 'models', 'media_files'):
        if key not in manifest:
            raise TransferError(f'В {MANIFEST_NAME} отсутствует обязательное поле "{key}".')

    data_info = manifest['data_file']
    if not isinstance(data_info, dict) or 'sha256' not in data_info:
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "data_file".')

    data_path = bundle_path / DATA_NAME
    if not data_path.is_file():
        raise TransferError(f'В пакете нет {DATA_NAME}.')
    data_text = data_path.read_text(encoding='utf-8')
    actual_data_hash = text_sha256(data_text)
    if actual_data_hash != data_info['sha256']:
        raise TransferError(
            f'Контрольная сумма {DATA_NAME} не совпадает: ожидалось '
            f'{data_info["sha256"]}, фактически {actual_data_hash}.'
        )
    try:
        records = json.loads(data_text)
    except json.JSONDecodeError as exc:
        raise TransferError(f'{DATA_NAME} повреждён: {exc}.') from exc
    if not isinstance(records, list):
        raise TransferError(f'{DATA_NAME} должен содержать список объектов.')

    models_info = manifest['models']
    if not isinstance(models_info, dict):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "models".')
    manifest_labels = set(models_info)
    expected_labels = set(TRANSFERABLE_MODELS)
    missing_labels = sorted(expected_labels - manifest_labels)
    unexpected_labels = sorted(manifest_labels - expected_labels)
    if missing_labels:
        raise TransferError('В пакете нет данных по моделям: ' + ', '.join(missing_labels) + '.')
    if unexpected_labels:
        raise TransferError(
            'Пакет содержит неизвестные модели: ' + ', '.join(unexpected_labels) + '.'
        )

    declared_total = sum(entry.get('count', 0) for entry in models_info.values())
    if declared_total != len(records):
        raise TransferError(
            f'{DATA_NAME} содержит {len(records)} записей, а manifest заявляет {declared_total}.'
        )

    media_root = bundle_path / MEDIA_DIR_NAME
    media_files = manifest['media_files']
    if not isinstance(media_files, list):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "media_files".')
    for entry in media_files:
        relative = entry.get('path')
        stored = resolve_inside(media_root, relative)
        if not stored.is_file():
            raise TransferError(f'В пакете нет файла вложения: {relative}.')
        actual_size = stored.stat().st_size
        if actual_size != entry.get('size'):
            raise TransferError(
                f'Размер файла {relative} не совпадает: ожидалось {entry.get("size")}, '
                f'фактически {actual_size}.'
            )
        actual_hash = file_sha256(stored)
        if actual_hash != entry.get('sha256'):
            raise TransferError(
                f'Контрольная сумма файла {relative} не совпадает: ожидалось '
                f'{entry.get("sha256")}, фактически {actual_hash}.'
            )

    result = {
        'manifest': manifest,
        'record_count': len(records),
        'media_count': len(media_files),
        'missing_media': manifest.get('missing_media', []),
        'warnings': list(manifest.get('warnings', [])),
        'migration_state_matches': None,
    }

    if check_migration_state:
        current = get_applied_migration_state()
        bundle_applied = set((manifest.get('migration_state') or {}).get('applied', []))
        current_applied = set(current['applied'])
        only_in_bundle = sorted(bundle_applied - current_applied)
        if only_in_bundle:
            raise TransferError(
                'В целевой базе не применены миграции, присутствовавшие в источнике: '
                + ', '.join(only_in_bundle) + '.'
            )
        only_in_target = sorted(current_applied - bundle_applied)
        if only_in_target:
            result['warnings'].append(
                'В целевой базе применены дополнительные миграции: ' + ', '.join(only_in_target) + '.'
            )
        result['migration_state_matches'] = not only_in_bundle and not only_in_target

    return result


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def check_import_preconditions(bundle_dir):
    """Validate everything that must hold before a single row is written."""
    if connection.vendor != 'postgresql':
        raise TransferError(
            f'Импорт разрешён только на PostgreSQL, текущий backend — {connection.vendor}.'
        )
    if getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        raise TransferError(
            'Импорт запрещён при EMAIL_NOTIFICATIONS_ENABLED=true. '
            'Отключите отправку email на время переноса.'
        )
    require_all_migrations_applied()
    validation = validate_bundle(bundle_dir, check_migration_state=True)

    non_empty = find_non_empty_models()
    if non_empty:
        seeded = [label for label in non_empty if label in MIGRATION_SEEDED_MODELS]
        other = [label for label in non_empty if label not in MIGRATION_SEEDED_MODELS]
        details = []
        if other:
            details.append('содержат данные: ' + ', '.join(other))
        if seeded:
            details.append(
                'заполнены миграциями данных: ' + ', '.join(seeded)
                + ' (пакет содержит собственные копии этих строк; очистите их вручную)'
            )
        raise TransferError(
            'Импорт возможен только в пустые переносимые таблицы. Непустые таблицы — '
            + '; '.join(details)
            + '. Команда ничего не удаляет самостоятельно.'
        )

    media_root = Path(settings.MEDIA_ROOT)
    if validation['media_count'] and not directory_is_empty(media_root):
        raise TransferError(
            f'Каталог MEDIA_ROOT {media_root} не пуст. Перезапись существующих файлов '
            'не выполняется автоматически: перенесите или очистите каталог вручную.'
        )
    return validation


def plan_import(bundle_dir):
    """Return the list of actions a real import would perform."""
    validation = check_import_preconditions(bundle_dir)
    manifest = validation['manifest']
    actions = [
        f'Проверить пакет {Path(bundle_dir).resolve()} (выполнено).',
        f'Загрузить {validation["record_count"]} записей в рамках одной транзакции.',
    ]
    for label in TRANSFERABLE_MODELS:
        count = manifest['models'][label]['count']
        actions.append(f'  {label}: {count} записей.')
    actions.append(f'Скопировать {validation["media_count"]} файлов в {settings.MEDIA_ROOT}.')
    actions.append('Восстановить последовательности PostgreSQL для моделей с AutoField.')
    actions.append('Синхронизировать ActNumberSequence по фактическим номерам актов.')
    return validation, actions


def import_bundle(bundle_dir):
    """Import a validated bundle into an empty PostgreSQL database."""
    validation = check_import_preconditions(bundle_dir)
    bundle_path = Path(bundle_dir)
    media_root = Path(settings.MEDIA_ROOT)

    staging_parent = tempfile.mkdtemp(prefix='migration-bundle-import-')
    staging_media = Path(staging_parent) / MEDIA_DIR_NAME
    try:
        staging_media.mkdir(parents=True)
        for entry in validation['manifest']['media_files']:
            source = resolve_inside(bundle_path / MEDIA_DIR_NAME, entry['path'])
            destination = resolve_inside(staging_media, entry['path'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)

        data_text = (bundle_path / DATA_NAME).read_text(encoding='utf-8')
        loaded = 0
        with transaction.atomic():
            for deserialized in serializers.deserialize(
                'json', data_text, handle_forward_references=False
            ):
                # save() uses raw=True, so post_save receivers stay inert and no
                # workflow service or notification is triggered.
                deserialized.save()
                loaded += 1
            sequence_result = reset_database_sequences()
        media_result = _activate_media(staging_media, media_root)
        number_sequence_result = sync_act_number_sequences()
    except Exception:
        shutil.rmtree(staging_parent, ignore_errors=True)
        raise
    else:
        shutil.rmtree(staging_parent, ignore_errors=True)

    return {
        'loaded': loaded,
        'sequences': sequence_result,
        'media': media_result,
        'act_number_sequences': number_sequence_result,
        'validation': validation,
    }


def _activate_media(staging_media, media_root):
    """Move prepared files into MEDIA_ROOT after the database is committed."""
    copied = []
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        for source in sorted(p for p in staging_media.rglob('*') if p.is_file()):
            relative = source.relative_to(staging_media).as_posix()
            destination = resolve_inside(media_root, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.append(relative)
    except (OSError, TransferError) as exc:
        raise TransferError(
            'База данных импортирована и зафиксирована, но активировать каталог media '
            f'не удалось: {exc}. Скопируйте файлы из пакета в {media_root} вручную '
            'и повторно запустите verify_migration_bundle.'
        ) from exc
    return {'copied': copied}


def reset_database_sequences():
    """Reset auto-increment sequences using the backend's own SQL.

    Sequence names are produced by Django, never hardcoded. On a backend
    without sequences (SQLite) this is a documented no-op.
    """
    models_with_auto_pk = [
        model
        for model in get_transferable_models()
        if isinstance(model._meta.pk, (models.AutoField, models.BigAutoField, models.SmallAutoField))
    ]
    statements = connection.ops.sequence_reset_sql(no_style(), models_with_auto_pk)
    if statements:
        with connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    return {
        'models': [model._meta.label for model in models_with_auto_pk],
        'statements': len(statements),
    }


def sync_act_number_sequences():
    """Raise every yearly act counter to the highest real number in that year.

    Idempotent: an already-correct counter is left alone and a counter is never
    lowered. Non-standard historical numbers are ignored.
    """
    Act = apps.get_model('acts.Act')
    ActNumberSequence = apps.get_model('acts.ActNumberSequence')

    highest = {}
    for number in Act._default_manager.values_list('number', flat=True).iterator():
        match = ACT_NUMBER_PATTERN.match((number or '').strip())
        if not match:
            continue
        year = int(match.group(1))
        value = int(match.group(2))
        if value > highest.get(year, 0):
            highest[year] = value

    created = []
    raised = []
    unchanged = []
    for year, value in sorted(highest.items()):
        with transaction.atomic():
            sequence, was_created = ActNumberSequence.objects.get_or_create(
                year=year, defaults={'last_value': value}
            )
            if was_created:
                created.append(year)
                continue
            locked = ActNumberSequence.objects.select_for_update().get(pk=sequence.pk)
            if locked.last_value < value:
                locked.last_value = value
                locked.save(update_fields=['last_value'])
                raised.append(year)
            else:
                unchanged.append(year)
    return {'created': created, 'raised': raised, 'unchanged': unchanged}


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def verify_against_bundle(bundle_dir):
    """Compare the current database and MEDIA_ROOT against a bundle."""
    validation = validate_bundle(bundle_dir)
    manifest = validation['manifest']
    differences = []

    current_stats, _combined = collect_model_stats()
    model_report = {}
    for label in TRANSFERABLE_MODELS:
        expected = manifest['models'][label]
        actual = current_stats[label]
        entry = {
            'expected_count': expected.get('count'),
            'actual_count': actual['count'],
            'expected_max_pk': expected.get('max_pk'),
            'actual_max_pk': actual['max_pk'],
            'expected_hash': expected.get('hash'),
            'actual_hash': actual['hash'],
            'matches': True,
        }
        if entry['expected_count'] != entry['actual_count']:
            entry['matches'] = False
            differences.append(
                f'{label}: количество записей {entry["actual_count"]} вместо {entry["expected_count"]}.'
            )
        if entry['expected_max_pk'] != entry['actual_max_pk']:
            entry['matches'] = False
            differences.append(
                f'{label}: максимальный PK {entry["actual_max_pk"]} вместо {entry["expected_max_pk"]}.'
            )
        if entry['expected_hash'] != entry['actual_hash']:
            entry['matches'] = False
            differences.append(f'{label}: хеш данных не совпадает.')
        model_report[label] = entry

    media_report = _verify_media(manifest)
    differences.extend(media_report['differences'])

    relations = check_relational_invariants()
    differences.extend(relations['problems'])

    report = {
        'checked_at': datetime.now(dt_timezone.utc).isoformat(),
        'bundle': str(Path(bundle_dir).resolve()),
        'target_vendor': connection.vendor,
        'models': model_report,
        'media': media_report,
        'relations': relations,
        'differences': differences,
        'ok': not differences,
    }
    return report


def _verify_media(manifest):
    media_root = Path(settings.MEDIA_ROOT)
    differences = []
    checked = 0
    for entry in manifest.get('media_files', []):
        relative = entry['path']
        try:
            target = resolve_inside(media_root, relative)
        except UnsafePathError as exc:
            differences.append(str(exc))
            continue
        if not target.is_file():
            differences.append(f'media: файл отсутствует — {relative}.')
            continue
        checked += 1
        actual_size = target.stat().st_size
        if actual_size != entry['size']:
            differences.append(
                f'media: размер {relative} равен {actual_size} вместо {entry["size"]}.'
            )
        actual_hash = file_sha256(target)
        if actual_hash != entry['sha256']:
            differences.append(f'media: контрольная сумма {relative} не совпадает.')
    return {
        'expected': len(manifest.get('media_files', [])),
        'checked': checked,
        'differences': differences,
    }


def check_relational_invariants():
    """Check the minimum set of cross-model invariants after an import."""
    problems = []

    def orphans(label, field, related_label):
        model = apps.get_model(label)
        related = apps.get_model(related_label)
        related_ids = set(related._default_manager.values_list('pk', flat=True))
        column = f'{field}_id'
        bad = [
            pk
            for pk, value in model._default_manager.values_list('pk', column)
            if value is not None and value not in related_ids
        ]
        if bad:
            problems.append(
                f'{label}.{field}: потерянные ссылки для записей {sorted(bad)[:10]}.'
            )
        return bad

    orphans('accounts.UserProfile', 'user', 'auth.User')
    orphans('acts.ActDefect', 'act', 'acts.Act')
    orphans('acts.ActHistoryEvent', 'act', 'acts.Act')
    orphans('acts.ActComment', 'act', 'acts.Act')
    orphans('acts.ActAttachment', 'act', 'acts.Act')
    orphans('acts.ActRootAnalysis', 'act', 'acts.Act')
    orphans('acts.ActCorrectiveAction', 'root_analysis', 'acts.ActRootAnalysis')
    orphans('acts.ActCorrectiveActionAssignee', 'corrective_action', 'acts.ActCorrectiveAction')
    orphans('acts.ActCorrectiveActionAssignee', 'user', 'auth.User')
    orphans('tasks.Task', 'source_action', 'acts.ActCorrectiveAction')
    orphans('tasks.Task', 'act', 'acts.Act')
    orphans('tasks.Task', 'root_analysis', 'acts.ActRootAnalysis')
    orphans('tasks.TaskAssignee', 'task', 'tasks.Task')
    orphans('tasks.TaskAssignee', 'user', 'auth.User')
    orphans('notifications.Notification', 'related_act', 'acts.Act')
    orphans('notifications.NotificationDelivery', 'notification', 'notifications.Notification')

    Task = apps.get_model('tasks.Task')
    inconsistent = []
    for task_pk, act_id, root_id, action_id in Task._default_manager.values_list(
        'pk', 'act_id', 'root_analysis_id', 'source_action_id'
    ):
        action = apps.get_model('acts.ActCorrectiveAction')._default_manager.filter(
            pk=action_id
        ).values_list('root_analysis_id', flat=True).first()
        if action is None:
            continue
        root_act = apps.get_model('acts.ActRootAnalysis')._default_manager.filter(
            pk=action
        ).values_list('act_id', flat=True).first()
        if action != root_id or root_act != act_id:
            inconsistent.append(task_pk)
    if inconsistent:
        problems.append(
            f'tasks.Task: несогласованные act/root_analysis/source_action для {sorted(inconsistent)[:10]}.'
        )

    Act = apps.get_model('acts.Act')
    seen_numbers = {}
    duplicates = set()
    for number in Act._default_manager.values_list('number', flat=True):
        if not ACT_NUMBER_PATTERN.match((number or '').strip()):
            continue
        if number in seen_numbers:
            duplicates.add(number)
        seen_numbers[number] = True
    if duplicates:
        problems.append('acts.Act: повторяющиеся стандартные номера — ' + ', '.join(sorted(duplicates)) + '.')

    ActNumberSequence = apps.get_model('acts.ActNumberSequence')
    counters = dict(ActNumberSequence._default_manager.values_list('year', 'last_value'))
    highest = {}
    for number in Act._default_manager.values_list('number', flat=True):
        match = ACT_NUMBER_PATTERN.match((number or '').strip())
        if not match:
            continue
        year, value = int(match.group(1)), int(match.group(2))
        highest[year] = max(highest.get(year, 0), value)
    lagging = sorted(
        year for year, value in highest.items() if counters.get(year, 0) < value
    )
    if lagging:
        problems.append(
            'acts.ActNumberSequence: счётчики отстают от фактических номеров за годы '
            + ', '.join(str(year) for year in lagging) + '.'
        )

    return {'problems': problems, 'act_number_years': sorted(highest)}
