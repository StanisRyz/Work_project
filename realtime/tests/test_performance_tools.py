"""STAB-2 performance tooling: the profiling command and the dataset generator.

These verify the *safety* contract of the tools — read-only, dry-run by
default, no business content in a report — rather than any particular timing,
which depends on the machine and the database.
"""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from acts.models import Act, ActComment
from notifications.models import Notification
from tasks.models import Task

from .base import RealtimeFixtureMixin


class ProfileRealtimeSyncCommandTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def _run(self, **options):
        buffer = StringIO()
        call_command('profile_realtime_sync', stdout=buffer, **options)
        return buffer.getvalue()

    def test_it_reports_the_vendor_role_and_a_query_count_per_scenario(self):
        self.make_act(self.status_created)

        output = self._run(user=self.otk_user.pk, repeat=2)

        self.assertIn(connection.vendor, output)
        self.assertIn('realtime_sync', output)
        self.assertIn('revision_notifications', output)
        self.assertIn('act_registry', output)

    def test_an_unknown_user_is_a_clear_command_error(self):
        with self.assertRaises(CommandError):
            self._run(user=99999, repeat=1)

    def test_a_non_positive_repeat_is_refused(self):
        with self.assertRaises(CommandError):
            self._run(user=self.otk_user.pk, repeat=0)

    def test_profiling_changes_nothing_in_the_database(self):
        act = self.make_act(self.status_created)
        Notification.objects.create(
            recipient=self.otk_user,
            actor=self.ko_user,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Заголовок',
            message='Сообщение',
            related_act=act,
            deduplication_key='profile-readonly',
        )
        before = (Act.objects.count(), Notification.objects.count(), ActComment.objects.count())

        self._run(user=self.otk_user.pk, repeat=3)

        after = (Act.objects.count(), Notification.objects.count(), ActComment.objects.count())
        self.assertEqual(before, after)
        self.assertFalse(Notification.objects.filter(is_read=True).exists())

    def test_the_json_report_carries_no_names_addresses_or_business_text(self):
        act = self.make_act(self.status_created)
        Notification.objects.create(
            recipient=self.otk_user,
            actor=self.ko_user,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Секретный заголовок',
            message='Секретное сообщение',
            related_act=act,
            deduplication_key='profile-report',
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'report.json'
            self._run(user=self.otk_user.pk, repeat=2, json_report=str(path))
            raw = path.read_text(encoding='utf-8')
            report = json.loads(raw)

        self.assertEqual(report['database_vendor'], connection.vendor)
        self.assertEqual(report['user_role'], 'otk')
        self.assertTrue(report['measurements'])
        # Nothing identifying and nothing from the business objects.
        self.assertNotIn(self.otk_user.username, raw)
        self.assertNotIn(act.number, raw)
        self.assertNotIn('Секретный', raw)
        self.assertNotIn('redis', raw.lower())

    def test_the_measured_query_count_matches_the_documented_budget(self):
        self.make_act(self.status_created)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'budget.json'
            self._run(user=self.otk_user.pk, repeat=2, json_report=str(path))
            report = json.loads(path.read_text(encoding='utf-8'))

        by_scenario = {row['scenario']: row for row in report['measurements']}
        self.assertEqual(by_scenario['realtime_sync']['queries'], 9)
        self.assertEqual(by_scenario['revision_notifications']['queries'], 1)
        for name in ('revision_tasks', 'revision_acts', 'revision_comments', 'revision_activities'):
            self.assertEqual(by_scenario[name]['queries'], 2, name)
        self.assertTrue(all(row['queries_stable'] for row in report['measurements']))

    def test_explain_is_refused_outside_postgresql(self):
        if connection.vendor == 'postgresql':
            self.skipTest('This asserts the SQLite/other-backend refusal message.')

        output = self._run(user=self.otk_user.pk, repeat=1, explain=True)

        self.assertIn('EXPLAIN пропущен', output)
        self.assertIn(connection.vendor, output)

    def test_explain_analyze_is_also_refused_outside_postgresql(self):
        if connection.vendor == 'postgresql':
            self.skipTest('This asserts the SQLite/other-backend refusal message.')

        output = self._run(user=self.otk_user.pk, repeat=1, explain_analyze=True)

        self.assertIn('EXPLAIN пропущен', output)


class SeedPerformanceDatasetCommandTests(TestCase):
    def _run(self, **options):
        buffer = StringIO()
        call_command('seed_performance_dataset', stdout=buffer, **options)
        return buffer.getvalue()

    @override_settings(DEBUG=True)
    def test_it_is_a_dry_run_by_default_and_writes_nothing(self):
        output = self._run(users=2, acts=3, tasks=1, comments=2, history=2)

        self.assertIn('DRY-RUN', output)
        self.assertEqual(Act.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_it_refuses_to_run_when_debug_is_off(self):
        with self.assertRaises(CommandError) as ctx:
            self._run(users=1, acts=1, tasks=0, comments=0, history=0, execute=True)

        self.assertIn('DEBUG=False', str(ctx.exception))
        self.assertEqual(Act.objects.count(), 0)

    @override_settings(DEBUG=False)
    def test_the_explicit_override_allows_a_prepared_test_environment(self):
        self._run(
            users=2,
            acts=2,
            tasks=0,
            comments=1,
            history=1,
            notifications_per_user=1,
            execute=True,
            i_know_this_is_not_development=True,
        )

        self.assertEqual(Act.objects.count(), 2)

    @override_settings(DEBUG=True)
    def test_everything_it_creates_is_marked_as_synthetic(self):
        self._run(
            users=2,
            acts=3,
            tasks=0,
            comments=2,
            history=2,
            notifications_per_user=1,
            execute=True,
        )

        self.assertEqual(Act.objects.count(), 3)
        for act in Act.objects.all():
            self.assertTrue(act.number.startswith('PERF-SYNTHETIC'), act.number)
            self.assertIn('PERF-SYNTHETIC', act.nomenclature)
        for comment in ActComment.objects.all():
            self.assertIn('PERF-SYNTHETIC', comment.text)

    @override_settings(DEBUG=True)
    def test_the_generated_accounts_have_no_usable_password(self):
        self._run(users=2, acts=1, tasks=0, comments=0, history=0, execute=True)

        from django.contrib.auth import get_user_model

        generated = get_user_model().objects.filter(username__startswith='perf_user_')
        self.assertEqual(generated.count(), 2)
        for user in generated:
            self.assertFalse(user.has_usable_password())


class SseDbConnectionCommandTests(TestCase):
    def test_it_refuses_to_pretend_sqlite_has_postgresql_connections(self):
        if connection.vendor == 'postgresql':
            self.skipTest('The refusal only applies to non-PostgreSQL backends.')

        with self.assertRaises(CommandError) as ctx:
            call_command('check_sse_db_connections', stdout=StringIO())

        self.assertIn('pg_stat_activity', str(ctx.exception))
        self.assertIn(connection.vendor, str(ctx.exception))
