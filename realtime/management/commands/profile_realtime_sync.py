"""Measure what `/realtime/sync/` and the live list endpoints actually cost.

Read-only by design: every measured call is a `SELECT`, and the command opens
no transaction of its own that could write anything. It exists so a performance
claim can be backed by a number from a real database instead of an assumption.

The report is deliberately free of business content. It records how many
queries ran and how long they took, the database vendor and the profiled user's
*role* — never a username, an email, an act number, a task text, a comment or
any setting that could carry a secret.
"""

import json
import statistics
import time
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, reset_queries


# Every measured operation, in the order a page would trigger them. Each entry
# is a callable taking the user and returning something (discarded) — the point
# is the queries it issues, not the value.
def _scenarios():
    from acts.selectors import build_act_list_state
    from realtime import sync as sync_service
    from tasks.selectors import build_task_list_state

    def full_sync(user):
        return sync_service.build_sync_state(user)

    def notifications_revision(user):
        return sync_service._notifications_revision(user)

    def tasks_revision(user):
        return sync_service._tasks_revision(user)

    def acts_revision(user):
        return sync_service._acts_revision(user)

    def comments_revision(user):
        return sync_service._comments_revision(user)

    def activities_revision(user):
        return sync_service._activities_revision(user)

    def task_list(user):
        from django.http import QueryDict

        state = build_task_list_state(user, QueryDict('tab=my'))
        # The registry renders rows, so the queryset must actually be walked
        # for the measurement to mean anything.
        return len(list(state['tasks']))

    def act_registry(user):
        from django.http import QueryDict

        state = build_act_list_state(user, QueryDict(''))
        return len(list(state['acts']))

    return [
        ('realtime_sync', full_sync),
        ('revision_notifications', notifications_revision),
        ('revision_tasks', tasks_revision),
        ('revision_acts', acts_revision),
        ('revision_comments', comments_revision),
        ('revision_activities', activities_revision),
        ('task_list', task_list),
        ('act_registry', act_registry),
    ]


def _percentile(values, fraction):
    """Nearest-rank percentile; `statistics.quantiles` needs more samples."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[index]


class Command(BaseCommand):
    help = (
        'Профилирует /realtime/sync/ и связанные запросы для одного '
        'пользователя: число SQL-запросов и задержки. Только чтение; отчёт не '
        'содержит имён, адресов, текстов и секретов.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--user', type=int, required=True, help='Идентификатор пользователя.')
        parser.add_argument(
            '--repeat', type=int, default=10, help='Сколько раз повторить каждый сценарий (по умолчанию 10).'
        )
        parser.add_argument('--json-report', help='Куда записать безопасный JSON-отчёт.')
        parser.add_argument(
            '--explain',
            action='store_true',
            help='Показать план запросов. Только PostgreSQL, без ANALYZE.',
        )
        parser.add_argument(
            '--explain-analyze',
            action='store_true',
            help=(
                'Выполнить EXPLAIN (ANALYZE, BUFFERS). Требует отдельного явного '
                'флага, так как действительно выполняет запрос.'
            ),
        )

    def handle(self, *args, **options):
        repeat = options['repeat']
        if repeat <= 0:
            raise CommandError('--repeat должен быть положительным.')

        user = self._get_user(options['user'])
        vendor = connection.vendor
        is_postgresql = vendor == 'postgresql'

        self.stdout.write(f'Backend:      {vendor}')
        self.stdout.write(f'Роль:         {self._role_of(user) or "—"}')
        self.stdout.write(f'Повторов:     {repeat}')
        self.stdout.write('')

        # Django only records queries when DEBUG is on; force it for the
        # measurement and restore it immediately afterwards.
        previous_debug = settings.DEBUG
        settings.DEBUG = True
        try:
            measurements = [
                self._measure(name, operation, user, repeat)
                for name, operation in _scenarios()
            ]
        finally:
            settings.DEBUG = previous_debug
            reset_queries()

        self._print_table(measurements)

        explain_output = None
        if options['explain'] or options['explain_analyze']:
            explain_output = self._explain(user, is_postgresql, options['explain_analyze'])

        if options['json_report']:
            self._write_report(
                options['json_report'], vendor, user, repeat, measurements, explain_output
            )
        return None

    # -- measurement -------------------------------------------------------

    def _measure(self, name, operation, user, repeat):
        # One untimed warm-up: the first call also resolves `user.userprofile`
        # and fills Django's model cache, which is request setup rather than
        # the cost of the operation itself.
        operation(user)

        durations = []
        query_counts = []
        for _ in range(repeat):
            reset_queries()
            started = time.perf_counter()
            operation(user)
            durations.append((time.perf_counter() - started) * 1000)
            query_counts.append(len(connection.queries))

        return {
            'scenario': name,
            'queries': max(query_counts),
            'queries_stable': len(set(query_counts)) == 1,
            'min_ms': round(min(durations), 2),
            'avg_ms': round(statistics.fmean(durations), 2),
            'p95_ms': round(_percentile(durations, 0.95), 2),
            'max_ms': round(max(durations), 2),
        }

    def _print_table(self, measurements):
        header = f'{"Сценарий":<24}{"Запросы":>9}{"min":>10}{"avg":>10}{"p95":>10}{"max":>10}'
        self.stdout.write(header)
        self.stdout.write('-' * len(header))
        for row in measurements:
            marker = '' if row['queries_stable'] else ' (!)'
            self.stdout.write(
                f'{row["scenario"]:<24}{row["queries"]:>9}'
                f'{row["min_ms"]:>10.2f}{row["avg_ms"]:>10.2f}'
                f'{row["p95_ms"]:>10.2f}{row["max_ms"]:>10.2f}{marker}'
            )
        if any(not row['queries_stable'] for row in measurements):
            self.stdout.write('')
            self.stdout.write(
                '(!) число запросов менялось между повторами — обычно это признак '
                'ленивой загрузки, зависящей от данных.'
            )

    # -- EXPLAIN -----------------------------------------------------------

    def _explain(self, user, is_postgresql, analyze):
        self.stdout.write('')
        if not is_postgresql:
            # Never let a SQLite plan masquerade as a PostgreSQL measurement.
            self.stdout.write(
                'EXPLAIN пропущен: план выполнения PostgreSQL недоступен на '
                f'backend "{connection.vendor}". Планы и EXPLAIN ANALYZE имеют '
                'смысл только на настоящей PostgreSQL — результаты SQLite '
                'нельзя выдавать за производительность PostgreSQL.'
            )
            return None

        from acts.permissions import get_all_visible_acts_queryset
        from tasks.permissions import get_visible_tasks_queryset

        plans = {}
        targets = {
            'visible_acts': get_all_visible_acts_queryset(user),
            'visible_tasks': get_visible_tasks_queryset(user),
        }
        options = {'ANALYZE': True, 'BUFFERS': True} if analyze else {}
        label = 'EXPLAIN (ANALYZE, BUFFERS)' if analyze else 'EXPLAIN'
        self.stdout.write(f'--- {label} ---')
        for name, queryset in targets.items():
            try:
                plan = queryset.explain(**options)
            except Exception as exc:  # noqa: BLE001 - a plan is diagnostic only
                plan = f'{type(exc).__name__}'
            plans[name] = plan
            self.stdout.write('')
            self.stdout.write(f'[{name}]')
            self.stdout.write(str(plan))
        return {'mode': label, 'plans': plans}

    # -- report ------------------------------------------------------------

    def _write_report(self, path, vendor, user, repeat, measurements, explain_output):
        report = {
            'schema_version': 1,
            # No username, no email, no object identifiers: a role and a
            # database vendor are all the context a performance report needs.
            'database_vendor': vendor,
            'user_role': self._role_of(user) or '',
            'user_has_full_act_access': self._has_full_access(user),
            'repeat': repeat,
            'measurements': measurements,
            'explain': explain_output,
        }
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        self.stdout.write('')
        self.stdout.write(f'JSON-отчёт записан: {destination}')

    # -- helpers -----------------------------------------------------------

    def _get_user(self, user_id):
        try:
            return get_user_model().objects.select_related('userprofile').get(pk=user_id)
        except get_user_model().DoesNotExist as exc:
            raise CommandError(f'Пользователь {user_id} не найден.') from exc

    def _role_of(self, user):
        from acts.permissions import get_user_role

        return get_user_role(user)

    def _has_full_access(self, user):
        from acts.permissions import has_full_act_access

        return bool(has_full_act_access(user))
