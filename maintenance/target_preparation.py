"""Safe preparation of an *empty* test PostgreSQL database.

A freshly migrated database is not actually empty: two data migrations seed
`references.ActStatus` and `references.TaskStatus`. The migration bundle
carries its own copies of those rows, so they have to go before an import.

This service is the only place allowed to delete them, and it is deliberately
narrow:

* PostgreSQL only;
* dry-run by default;
* refuses outright if any user data exists anywhere in the transferable set;
* deletes only the exact, pre-listed reference codes created by migrations;
* never calls `flush`, never touches users, acts, tasks, notifications or files.
"""

from django.apps import apps
from django.db import connection, transaction

from .database_transfer import (
    MIGRATION_SEEDED_MODELS,
    MIGRATION_SEEDED_ROWS,
    TRANSFERABLE_MODELS,
    TransferError,
)


CONFIRMATION_PHRASE = 'ОЧИСТИТЬ ТЕСТОВУЮ БАЗУ'


def inspect_target():
    """Return what is currently in the target, split into user data and seeds."""
    user_data = []
    seeded = []
    removable = []
    unexpected_seed_rows = []

    for label in TRANSFERABLE_MODELS:
        model = apps.get_model(label)
        count = model._default_manager.count()
        if not count:
            continue
        entry = {'model': label, 'table': model._meta.db_table, 'rows': count}
        if label in MIGRATION_SEEDED_MODELS:
            allowed_codes = MIGRATION_SEEDED_ROWS[label]
            allowed = model._default_manager.filter(code__in=allowed_codes)
            other = model._default_manager.exclude(code__in=allowed_codes)
            other_codes = sorted(other.values_list('code', flat=True))
            entry['allowed_codes'] = sorted(allowed.values_list('code', flat=True))
            entry['other_codes'] = other_codes
            seeded.append(entry)
            if other_codes:
                unexpected_seed_rows.append(entry)
            if entry['allowed_codes']:
                removable.append(
                    {
                        'model': label,
                        'table': model._meta.db_table,
                        'codes': entry['allowed_codes'],
                        'rows': len(entry['allowed_codes']),
                    }
                )
        else:
            user_data.append(entry)

    return {
        'user_data': user_data,
        'seeded': seeded,
        'removable': removable,
        'unexpected_seed_rows': unexpected_seed_rows,
    }


def prepare_empty_target(execute=False, confirmation=''):
    """Report — and optionally perform — the narrow reference-row cleanup."""
    if connection.vendor != 'postgresql':
        raise TransferError(
            f'Подготовка целевой базы разрешена только на PostgreSQL, текущий backend — '
            f'{connection.vendor}.'
        )

    inspection = inspect_target()
    result = {
        'vendor': connection.vendor,
        'dry_run': not execute,
        'user_data': inspection['user_data'],
        'seeded': inspection['seeded'],
        'planned': inspection['removable'],
        'deleted': [],
        'ready': False,
        'blocking': [],
    }

    if inspection['user_data']:
        details = ', '.join(f'{entry["model"]} ({entry["rows"]})' for entry in inspection['user_data'])
        result['blocking'].append(
            'Обнаружены пользовательские данные: ' + details
            + '. Команда ничего не удаляет: используйте отдельную пустую тестовую базу.'
        )
    if inspection['unexpected_seed_rows']:
        details = '; '.join(
            f'{entry["model"]}: {", ".join(entry["other_codes"])}'
            for entry in inspection['unexpected_seed_rows']
        )
        result['blocking'].append(
            'В справочниках есть строки вне списка миграций данных: ' + details
            + '. Такие строки не удаляются автоматически.'
        )

    if result['blocking']:
        return result

    if not execute:
        result['ready'] = not inspection['removable']
        return result

    if (confirmation or '').strip() != CONFIRMATION_PHRASE:
        raise TransferError(
            f'Удаление требует точного подтверждения. Повторите с --confirm "{CONFIRMATION_PHRASE}".'
        )

    with transaction.atomic():
        for entry in inspection['removable']:
            model = apps.get_model(entry['model'])
            deleted, _details = model._default_manager.filter(code__in=entry['codes']).delete()
            result['deleted'].append(
                {
                    'model': entry['model'],
                    'table': entry['table'],
                    'codes': entry['codes'],
                    'rows': deleted,
                }
            )

    remaining = [
        label
        for label in TRANSFERABLE_MODELS
        if apps.get_model(label)._default_manager.exists()
    ]
    result['ready'] = not remaining
    if remaining:
        result['blocking'].append(
            'После очистки остались непустые таблицы: ' + ', '.join(remaining) + '.'
        )
    return result
