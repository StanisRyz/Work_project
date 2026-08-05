"""Snapshot how many PostgreSQL connections this application is holding.

The question this answers: does a long-lived SSE stream keep a PostgreSQL
connection — and worse, an *idle in transaction* one — for its whole lifetime?
A stream lives for up to `REALTIME_MAX_CONNECTION_SECONDS`, so 50 open streams
holding a connection each would exhaust a default `max_connections` long before
the pilot reaches its user count.

Read-only: it queries `pg_stat_activity` and nothing else. Run it before,
during and after a `scripts/realtime_load_smoke.py` run and compare.

    python manage.py check_sse_db_connections --json-report before.json
    # start the load smoke in another terminal, then:
    python manage.py check_sse_db_connections --json-report during.json
    # stop the smoke run, wait a few seconds, then:
    python manage.py check_sse_db_connections --json-report after.json

The report holds counts and states only — never a query text, a user name, a
client address or any connection string.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection


# `state` values PostgreSQL reports. `idle in transaction` is the dangerous one:
# it pins both a connection and a transaction snapshot.
INTERESTING_STATES = ('active', 'idle', 'idle in transaction', 'idle in transaction (aborted)')


class Command(BaseCommand):
    help = (
        'Показывает число соединений PostgreSQL по состояниям (active, idle, '
        'idle in transaction) для текущей базы. Только чтение; в отчёт не '
        'попадают тексты запросов, имена пользователей и адреса.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--json-report', help='Куда записать безопасный JSON-отчёт.')
        parser.add_argument(
            '--label',
            default='',
            help='Метка замера, например before/during/after.',
        )

    def handle(self, *args, **options):
        if connection.vendor != 'postgresql':
            raise CommandError(
                f'Backend "{connection.vendor}" не поддерживает pg_stat_activity. '
                'Эта проверка имеет смысл только на настоящей PostgreSQL: на '
                'SQLite соединений в этом смысле не существует, и её результат '
                'нельзя выдавать за поведение PostgreSQL.'
            )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT state, count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                GROUP BY state
                """
            )
            rows = cursor.fetchall()
            cursor.execute('SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()')
            total = cursor.fetchone()[0]
            cursor.execute('SHOW max_connections')
            max_connections = int(cursor.fetchone()[0])

        by_state = {str(state or 'unknown'): int(count) for state, count in rows}
        report = {
            'schema_version': 1,
            'label': options['label'],
            'database_vendor': connection.vendor,
            'total_connections': total,
            'max_connections': max_connections,
            'by_state': {state: by_state.get(state, 0) for state in INTERESTING_STATES},
            'other_states': {
                state: count for state, count in by_state.items() if state not in INTERESTING_STATES
            },
        }

        if options['label']:
            self.stdout.write(f'Замер:                     {options["label"]}')
        self.stdout.write(f'Всего соединений:          {total} из {max_connections}')
        for state in INTERESTING_STATES:
            self.stdout.write(f'  {state:<28} {report["by_state"][state]}')
        for state, count in report['other_states'].items():
            self.stdout.write(f'  {state:<28} {count}')

        idle_in_transaction = report['by_state']['idle in transaction']
        if idle_in_transaction:
            self.stdout.write('')
            self.stdout.write(
                self.style.WARNING(
                    f'{idle_in_transaction} соединений в состоянии "idle in transaction". '
                    'Если это число растёт вместе с числом открытых SSE-потоков, '
                    'поток удерживает транзакцию — это и есть та проблема, ради '
                    'которой делается замер.'
                )
            )

        if options['json_report']:
            destination = Path(options['json_report'])
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8'
            )
            self.stdout.write('')
            self.stdout.write(f'JSON-отчёт записан: {destination}')
        return None
