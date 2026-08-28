"""Services for preparing a SQLite -> PostgreSQL data transfer.

The management commands in this app are thin wrappers around the functions
here: they parse arguments, print output and translate :class:`TransferError`
into ``CommandError``.

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


# Version 2 changed the manifest: per-model hashes use the canonical payload
# defined below (recomputable from data.json) and a safe source-media
# description was added. Version 1 bundles are deliberately rejected.
BUNDLE_FORMAT_VERSION = 2
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
# bundle carries its own copies, so the target must be cleared of them before
# importing; `prepare_empty_migration_target` is the only tool allowed to do it
# and only for the exact codes listed here.
MIGRATION_SEEDED_MODELS = (
    'accounts.Department',
    'references.ActStatus',
    'references.TaskStatus',
)

MIGRATION_SEEDED_ROWS = {
    # accounts.0003 / accounts.0005 create organisational departments.
    'accounts.Department': ('PDO', 'MAS'),
    # acts.0014 / acts.0015 create these two act statuses.
    'references.ActStatus': ('ARCHIVED', 'OTK_REVIEW'),
    # references.0002 / references.0003 create these two task statuses.
    'references.TaskStatus': ('COMPLETED', 'IN_PROGRESS'),
}

SHA256_PATTERN = re.compile(r'^[0-9a-f]{64}$')

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


def safe_path_label(path, keep=2):
    """Return a short, non-sensitive label for a filesystem path.

    Reports must never expose full server paths, so only the last few
    components are kept.
    """
    resolved = Path(path)
    parts = resolved.parts
    if len(parts) <= keep:
        return PurePosixPath(*parts).as_posix() if parts else ''
    return '.../' + '/'.join(parts[-keep:])


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


def describe_directory(path):
    """Return {file_count, total_size} for a directory tree."""
    root = Path(path)
    count = 0
    total = 0
    if root.is_dir():
        for entry in root.rglob('*'):
            if entry.is_file():
                count += 1
                total += entry.stat().st_size
    return {'file_count': count, 'total_size': total}


# --------------------------------------------------------------------------
# Media source selection
# --------------------------------------------------------------------------

def resolve_media_source(source_media_root=None):
    """Return the media directory an export must read from.

    Defaults to ``settings.MEDIA_ROOT``. An explicit override is normalized and
    validated exactly like every other filesystem path the tools accept: it
    must exist and be a directory.
    """
    default_root = Path(settings.MEDIA_ROOT)
    if source_media_root in (None, ''):
        chosen = default_root
        is_default = True
    else:
        chosen = Path(str(source_media_root)).expanduser()
        is_default = False
    try:
        resolved = chosen.resolve()
    except OSError as exc:
        raise TransferError(f'Не удалось разобрать путь к каталогу media: {exc}.') from exc
    if not resolved.exists():
        raise TransferError(f'Каталог media не найден: {safe_path_label(resolved)}.')
    if not resolved.is_dir():
        raise TransferError(f'Указанный путь media не является каталогом: {safe_path_label(resolved)}.')
    if not os.access(resolved, os.R_OK):
        raise TransferError(f'Каталог media недоступен для чтения: {safe_path_label(resolved)}.')
    try:
        is_default = is_default or resolved == default_root.resolve()
    except OSError:
        pass
    return resolved, is_default


def describe_media_source(resolved_root, is_default):
    """Safe manifest description of the media source — never an absolute path."""
    stats = describe_directory(resolved_root)
    return {
        'name': resolved_root.name,
        'is_default_media_root': bool(is_default),
        'file_count': stats['file_count'],
        'total_size': stats['total_size'],
    }


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


def canonical_model_payload(records):
    """Canonical text whose SHA-256 is a model's manifest hash.

    Deliberately independent of Django's own JSON framing so the very same
    value can be recomputed from `data.json` during validation.
    """
    return json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False)


def collect_model_stats():
    """Return {label: {count, max_pk, hash}} plus the combined object list."""
    stats = {}
    combined = []
    for label in TRANSFERABLE_MODELS:
        model = apps.get_model(label)
        records = json.loads(serialize_model(model))
        aggregate = model._default_manager.aggregate(value=models.Max('pk'))
        max_pk = aggregate['value']
        stats[label] = {
            'count': len(records),
            'max_pk': max_pk if isinstance(max_pk, int) else None,
            'hash': text_sha256(canonical_model_payload(records)),
        }
        combined.extend(records)
    return stats, combined


def recompute_model_stats(records):
    """Recompute per-model count / max PK / hash straight from data.json.

    Every record must belong to one of the allowed models; anything else means
    the bundle carries data the tooling never exports.
    """
    label_by_lowercase = {label.lower(): label for label in TRANSFERABLE_MODELS}
    buckets = {label: [] for label in TRANSFERABLE_MODELS}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise TransferError(f'{DATA_NAME}: запись #{index} не является объектом JSON.')
        raw_label = record.get('model')
        label = label_by_lowercase.get(str(raw_label).strip().lower())
        if label is None:
            raise TransferError(
                f'{DATA_NAME}: запись #{index} относится к неразрешённой модели {raw_label!r}.'
            )
        buckets[label].append(record)

    stats = {}
    for label, items in buckets.items():
        primary_keys = [
            item.get('pk')
            for item in items
            if isinstance(item.get('pk'), int) and not isinstance(item.get('pk'), bool)
        ]
        stats[label] = {
            'count': len(items),
            'max_pk': max(primary_keys) if primary_keys else None,
            'hash': text_sha256(canonical_model_payload(items)),
        }
    return stats


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

def export_bundle(output_dir, allow_missing_media=False, source_media_root=None):
    """Build a migration bundle from the current (SQLite) database.

    `source_media_root` selects the media directory to read attachments from;
    it defaults to ``settings.MEDIA_ROOT`` so a *copy* of media can be exported
    alongside a *copy* of the SQLite file.
    """
    if connection.vendor != 'sqlite':
        raise TransferError(
            f'Экспорт разрешён только на SQLite, текущий backend — {connection.vendor}.'
        )
    output_path = Path(output_dir)
    if not directory_is_empty(output_path):
        raise TransferError(
            f'Каталог {output_path} не пуст. Укажите новый или пустой каталог.'
        )
    media_root, media_is_default = resolve_media_source(source_media_root)
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
            staging / MEDIA_DIR_NAME, media_root, allow_missing_media
        )
        if missing_media:
            warnings.append(
                f'Пакет неполный: отсутствует файлов вложений — {len(missing_media)}. '
                'Обычный импорт такого пакета запрещён.'
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
            'source_media': describe_media_source(media_root, media_is_default),
            'media_files': media_files,
            'missing_media': missing_media,
            'complete': not missing_media,
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


def _copy_attachment_files(media_target, media_root, allow_missing_media):
    """Copy every file referenced by ActAttachment into the bundle."""
    ActAttachment = apps.get_model('acts.ActAttachment')
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
                    'Восстановите файл или запустите экспорт с --allow-missing-media '
                    '(такой пакет считается неполным).'
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


def _validate_model_order(manifest):
    model_order = manifest.get('model_order')
    if not isinstance(model_order, list) or not all(isinstance(item, str) for item in model_order):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "model_order".')
    duplicates = sorted({label for label in model_order if model_order.count(label) > 1})
    if duplicates:
        raise TransferError(
            'В "model_order" повторяются модели: ' + ', '.join(duplicates) + '.'
        )
    unknown = sorted(set(model_order) - set(TRANSFERABLE_MODELS))
    if unknown:
        raise TransferError(
            'В "model_order" перечислены неизвестные модели: ' + ', '.join(unknown) + '.'
        )
    if model_order != list(TRANSFERABLE_MODELS):
        raise TransferError(
            'Порядок моделей в пакете не совпадает с текущим TRANSFERABLE_MODELS. '
            'Пакет собран другой версией инструментов — пересоберите его.'
        )
    return model_order


def _validate_migration_state(manifest):
    state = manifest.get('migration_state')
    if not isinstance(state, dict):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "migration_state".')
    for key in ('applied', 'pending'):
        value = state.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TransferError(
                f'В {MANIFEST_NAME} блок "migration_state.{key}" должен быть списком строк.'
            )
    if state['pending']:
        raise TransferError(
            'Пакет собран из базы с непринятыми миграциями: '
            + ', '.join(state['pending']) + '. Пересоберите пакет после migrate.'
        )
    return state


def _validate_media_blocks(manifest, media_root):
    media_files = manifest.get('media_files')
    if not isinstance(media_files, list):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "media_files".')

    seen_paths = set()
    for index, entry in enumerate(media_files):
        if not isinstance(entry, dict):
            raise TransferError(f'В "media_files" запись #{index} не является объектом.')
        relative = normalize_relative_path(entry.get('path'))
        if relative in seen_paths:
            raise TransferError(f'В "media_files" повторяется путь {relative}.')
        seen_paths.add(relative)
        size = entry.get('size')
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise TransferError(f'В "media_files" некорректный размер для {relative}.')
        digest = entry.get('sha256')
        if not isinstance(digest, str) or not SHA256_PATTERN.match(digest):
            raise TransferError(f'В "media_files" некорректная контрольная сумма для {relative}.')

        stored = resolve_inside(media_root, relative)
        if not stored.is_file():
            raise TransferError(f'В пакете нет файла вложения: {relative}.')
        actual_size = stored.stat().st_size
        if actual_size != size:
            raise TransferError(
                f'Размер файла {relative} не совпадает: ожидалось {size}, фактически {actual_size}.'
            )
        actual_hash = file_sha256(stored)
        if actual_hash != digest:
            raise TransferError(
                f'Контрольная сумма файла {relative} не совпадает: ожидалось '
                f'{digest}, фактически {actual_hash}.'
            )

    missing_media = manifest.get('missing_media', [])
    if not isinstance(missing_media, list):
        raise TransferError(f'В {MANIFEST_NAME} некорректный блок "missing_media".')
    for index, entry in enumerate(missing_media):
        if not isinstance(entry, dict):
            raise TransferError(f'В "missing_media" запись #{index} не является объектом.')
        attachment_id = entry.get('attachment_id')
        if not isinstance(attachment_id, int) or isinstance(attachment_id, bool):
            raise TransferError(f'В "missing_media" запись #{index} без корректного attachment_id.')
        normalize_relative_path(entry.get('path'))
        if not isinstance(entry.get('reason'), str) or not entry['reason'].strip():
            raise TransferError(f'В "missing_media" запись #{index} без причины.')

    return media_files, missing_media


def validate_bundle(bundle_dir, check_migration_state=False):
    """Fully validate a bundle on disk. Raises TransferError on any problem.

    Every number in the manifest is recomputed from `data.json` and from the
    files actually present, so a manifest can never vouch for itself.
    """
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

    required = (
        'created_at',
        'source_vendor',
        'data_file',
        'models',
        'model_order',
        'migration_state',
        'media_files',
    )
    for key in required:
        if key not in manifest:
            raise TransferError(f'В {MANIFEST_NAME} отсутствует обязательное поле "{key}".')

    source_vendor = manifest['source_vendor']
    if source_vendor != 'sqlite':
        raise TransferError(
            f'Пакет собран не из SQLite (source_vendor={source_vendor!r}). '
            'Поддерживается перенос только из SQLite.'
        )

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

    _validate_model_order(manifest)
    migration_state = _validate_migration_state(manifest)

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

    recomputed = recompute_model_stats(records)
    for label in TRANSFERABLE_MODELS:
        declared = models_info[label]
        if not isinstance(declared, dict):
            raise TransferError(f'В "models" некорректная запись для {label}.')
        actual = recomputed[label]
        if declared.get('count') != actual['count']:
            raise TransferError(
                f'{label}: manifest заявляет {declared.get("count")} записей, '
                f'в {DATA_NAME} их {actual["count"]}.'
            )
        if declared.get('max_pk') != actual['max_pk']:
            raise TransferError(
                f'{label}: manifest заявляет максимальный PK {declared.get("max_pk")}, '
                f'в {DATA_NAME} — {actual["max_pk"]}.'
            )
        if declared.get('hash') != actual['hash']:
            raise TransferError(
                f'{label}: хеш данных в manifest не совпадает с пересчитанным по {DATA_NAME}.'
            )

    declared_total = sum(entry['count'] for entry in models_info.values())
    if declared_total != len(records):
        raise TransferError(
            f'{DATA_NAME} содержит {len(records)} записей, а manifest заявляет {declared_total}.'
        )

    media_files, missing_media = _validate_media_blocks(manifest, bundle_path / MEDIA_DIR_NAME)

    warnings = list(manifest.get('warnings', []))
    if missing_media:
        warnings.append(
            f'Пакет неполный: не хватает файлов вложений — {len(missing_media)}.'
        )

    result = {
        'manifest': manifest,
        'record_count': len(records),
        'media_count': len(media_files),
        'missing_media': missing_media,
        'complete': not missing_media,
        'warnings': warnings,
        'migration_state_matches': None,
        'recomputed_models': recomputed,
    }

    if check_migration_state:
        current = get_applied_migration_state()
        bundle_applied = set(migration_state['applied'])
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


def describe_missing_media(missing_media):
    """Return the full, printable list of attachments whose file is absent."""
    return [
        f'ActAttachment id={entry["attachment_id"]}: {entry["path"]} ({entry["reason"]})'
        for entry in missing_media
    ]


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def check_import_preconditions(bundle_dir, accept_missing_media=False):
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

    if validation['missing_media'] and not accept_missing_media:
        listed = describe_missing_media(validation['missing_media'])
        raise TransferError(
            'Пакет неполный: в нём отмечены отсутствующие файлы вложений ('
            + str(len(listed)) + '). Обычный импорт неполного пакета запрещён. '
            'Восстановите файлы и пересоберите пакет либо осознанно запустите импорт '
            'с --accept-missing-media. Отсутствуют: ' + '; '.join(listed) + '.'
        )

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
                + ' (пакет содержит собственные копии этих строк; очистите их командой '
                'prepare_empty_migration_target)'
            )
        raise TransferError(
            'Импорт возможен только в пустые переносимые таблицы. Непустые таблицы — '
            + '; '.join(details)
            + '. Команда ничего не удаляет самостоятельно.'
        )

    media_root = Path(settings.MEDIA_ROOT)
    if validation['media_count'] and not directory_is_empty(media_root):
        raise TransferError(
            f'Каталог MEDIA_ROOT {safe_path_label(media_root)} не пуст. Перезапись существующих '
            'файлов не выполняется автоматически: перенесите или очистите каталог вручную.'
        )
    return validation


def plan_import(bundle_dir, accept_missing_media=False):
    """Return the list of actions a real import would perform."""
    validation = check_import_preconditions(bundle_dir, accept_missing_media=accept_missing_media)
    manifest = validation['manifest']
    actions = [
        f'Проверить пакет {safe_path_label(bundle_dir)} (выполнено).',
        f'Загрузить {validation["record_count"]} записей в рамках одной транзакции.',
    ]
    for label in TRANSFERABLE_MODELS:
        count = manifest['models'][label]['count']
        actions.append(f'  {label}: {count} записей.')
    actions.append('Восстановить последовательности PostgreSQL для моделей с AutoField.')
    actions.append(
        f'После фиксации транзакции скопировать {validation["media_count"]} файлов в '
        f'{safe_path_label(settings.MEDIA_ROOT)}.'
    )
    if validation['missing_media']:
        actions.append(
            f'ВНИМАНИЕ: пакет неполный, отсутствующих файлов — {len(validation["missing_media"])}.'
        )
    return validation, actions


def import_bundle(bundle_dir, accept_missing_media=False):
    """Import a validated bundle into an empty PostgreSQL database.

    Loading the fixture and resetting PostgreSQL sequences both happen inside
    one `transaction.atomic()`: if either fails, every loaded row is rolled
    back. Media is activated only after that transaction is committed; a
    failure there yields a structured partial-success result instead of a bare
    success.
    """
    validation = check_import_preconditions(bundle_dir, accept_missing_media=accept_missing_media)
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
        # The transaction is committed from here on: media is activated last.
        media_result = _activate_media(staging_media, media_root)
    except Exception:
        shutil.rmtree(staging_parent, ignore_errors=True)
        raise
    else:
        shutil.rmtree(staging_parent, ignore_errors=True)

    result = {
        'status': 'ok' if media_result['ok'] else 'partial',
        'loaded': loaded,
        'sequences': sequence_result,
        'media': media_result,
        'validation': validation,
        'complete_bundle': validation['complete'],
        'missing_media': validation['missing_media'],
    }
    if not media_result['ok']:
        result['recovery'] = [
            'Не запускайте приложение на этой базе PostgreSQL: перенос не завершён.',
            f'Скопируйте файлы из {safe_path_label(bundle_path)}/{MEDIA_DIR_NAME} в '
            f'{safe_path_label(media_root)}, сохранив относительные пути.',
            f'Повторно запустите: python manage.py verify_migration_bundle --input '
            f'{safe_path_label(bundle_path)}.',
            'Повторный import_migration_bundle в эту базу невозможен — таблицы уже не пусты. '
            'Если восстановить media не удаётся, очистите целевую базу и MEDIA_ROOT '
            'и повторите перенос с нуля.',
        ]
    return result


def _activate_media(staging_media, media_root):
    """Move prepared files into MEDIA_ROOT after the database is committed.

    Never raises: a failure here happens *after* commit, so the caller needs a
    structured partial result rather than an exception that hides what landed.
    """
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
        return {
            'ok': False,
            'copied': copied,
            'error': (
                'База данных импортирована и зафиксирована, но активировать каталог media '
                f'не удалось: {exc}'
            ),
        }
    return {'ok': True, 'copied': copied, 'error': ''}


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


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------

def verify_against_bundle(bundle_dir, allow_missing_media=False):
    """Compare the current database and MEDIA_ROOT against a bundle.

    `missing_media` in the bundle is always a difference unless the operator
    explicitly runs the diagnostic mode that tolerates an incomplete transfer.
    """
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

    missing_media = validation['missing_media']
    missing_media_listed = describe_missing_media(missing_media)
    if missing_media and not allow_missing_media:
        differences.append(
            f'media: перенос неполный, в пакете отмечено отсутствующих файлов — '
            f'{len(missing_media)}: ' + '; '.join(missing_media_listed) + '.'
        )

    relations = check_relational_invariants()
    differences.extend(relations['problems'])

    report = {
        'checked_at': datetime.now(dt_timezone.utc).isoformat(),
        'bundle': safe_path_label(bundle_dir),
        'target_vendor': connection.vendor,
        'models': model_report,
        'media': media_report,
        'missing_media': missing_media_listed,
        'missing_media_allowed': bool(allow_missing_media),
        'complete_transfer': not missing_media,
        'relations': relations,
        'warnings': validation['warnings'],
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

    return {'problems': problems}
