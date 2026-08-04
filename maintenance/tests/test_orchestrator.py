"""Tests for `scripts/run_postgresql_rehearsal.py`.

The orchestrator is a standalone script — it deliberately does not import
Django — so it is loaded by path and driven with a fake stage runner. That
keeps the tests fast and lets them assert the two properties that matter most:
it stops at the first failing stage, and it always leaves a report behind.
"""

import importlib.util
import shutil
import sys
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


def load_orchestrator():
    path = Path(settings.BASE_DIR) / 'scripts' / 'run_postgresql_rehearsal.py'
    spec = importlib.util.spec_from_file_location('rehearsal_orchestrator', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


orchestrator = load_orchestrator()

SECRET_PASSWORD = 'super-secret-database-password'
SECRET_KEY_VALUE = 'super-secret-django-key'


class FakeRunner:
    """Records every stage and fails the one named in `fail_at`."""

    def __init__(self, fail_at=None, stdout='готово', stderr=''):
        self.fail_at = fail_at
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def __call__(self, name, arguments, environ):
        self.calls.append({'name': name, 'arguments': list(arguments), 'env': dict(environ)})
        if name == self.fail_at:
            return orchestrator.StageResult(
                name, ['manage.py', *arguments], 1, self.stdout, self.stderr, 0.25
            )
        return orchestrator.StageResult(
            name, ['manage.py', *arguments], 0, self.stdout, '', 0.25
        )


class OrchestratorTestCase(SimpleTestCase):
    def setUp(self):
        self.work_dir = Path(tempfile.mkdtemp(prefix='rehearsal-'))
        self.addCleanup(shutil.rmtree, self.work_dir, ignore_errors=True)

        self.source_db = self.work_dir / 'db-copy.sqlite3'
        self.source_db.write_bytes(b'SQLite format 3\x00')
        self.source_media = self.work_dir / 'media-copy'
        self.source_media.mkdir()
        (self.source_media / 'sample.txt').write_bytes(b'demo')
        self.bundle = self.work_dir / 'bundle'
        self.target_media = self.work_dir / 'target-media'
        self.json_report = self.work_dir / 'rehearsal-report.json'
        self.markdown_report = self.work_dir / 'rehearsal-report.md'

        self.environment = {
            'DB_NAME': 'quality_rehearsal',
            'DB_USER': 'quality_rehearsal',
            'DB_PASSWORD': SECRET_PASSWORD,
            'SECRET_KEY': SECRET_KEY_VALUE,
        }

    def argv(self, *extra):
        return [
            '--source-db', str(self.source_db),
            '--source-media', str(self.source_media),
            '--bundle', str(self.bundle),
            '--target-media', str(self.target_media),
            '--json-report', str(self.json_report),
            '--markdown-report', str(self.markdown_report),
            *extra,
        ]

    def run_main(self, runner, *extra, environment=None):
        return orchestrator.main(
            self.argv(*extra),
            runner=runner,
            environ=environment if environment is not None else self.environment,
            stream=StringIO(),
        )


class StageSequenceTests(OrchestratorTestCase):
    def test_all_nine_stages_run_in_the_documented_order(self):
        runner = FakeRunner()

        exit_code = self.run_main(runner)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [call['name'] for call in runner.calls],
            [
                'source_preflight',
                'export_bundle',
                'validate_bundle',
                'target_preflight',
                'import_dry_run',
                'import_bundle',
                'verify_bundle',
                'smoke_checks',
            ],
        )
        self.assertTrue(self.json_report.is_file())
        self.assertTrue(self.markdown_report.is_file())

    def test_each_stage_uses_the_backend_it_belongs_to(self):
        runner = FakeRunner()

        self.run_main(runner)

        backends = {call['name']: call['env']['DATABASE_ENGINE'] for call in runner.calls}
        self.assertEqual(backends['source_preflight'], 'sqlite')
        self.assertEqual(backends['export_bundle'], 'sqlite')
        self.assertEqual(backends['validate_bundle'], 'sqlite')
        self.assertEqual(backends['target_preflight'], 'postgresql')
        self.assertEqual(backends['import_bundle'], 'postgresql')
        self.assertEqual(backends['smoke_checks'], 'postgresql')

        sqlite_call = next(call for call in runner.calls if call['name'] == 'export_bundle')
        self.assertEqual(sqlite_call['env']['SQLITE_DB_PATH'], str(self.source_db.resolve()))
        postgres_call = next(call for call in runner.calls if call['name'] == 'import_bundle')
        self.assertEqual(
            postgres_call['env']['MEDIA_ROOT_PATH'], str(self.target_media.resolve())
        )
        self.assertNotIn('SQLITE_DB_PATH', postgres_call['env'])
        for call in runner.calls:
            self.assertEqual(call['env']['EMAIL_NOTIFICATIONS_ENABLED'], 'false')

    def test_execution_stops_at_the_first_failing_stage(self):
        runner = FakeRunner(fail_at='target_preflight', stderr='целевая база не пуста')

        exit_code = self.run_main(runner)

        self.assertEqual(exit_code, 1)
        self.assertEqual(
            [call['name'] for call in runner.calls],
            ['source_preflight', 'export_bundle', 'validate_bundle', 'target_preflight'],
        )

    def test_a_failed_stage_is_described_in_the_report(self):
        runner = FakeRunner(fail_at='import_bundle', stderr='ошибка импорта')

        self.run_main(runner)

        report = orchestrator.read_json(self.json_report)
        self.assertEqual(report['status'], 'failed')
        stages = {stage['name']: stage for stage in report['stages']}
        failed = stages['import_bundle']
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('ошибка импорта', failed['error'])
        self.assertIn('import_migration_bundle', failed['command'])
        self.assertGreaterEqual(failed['duration_seconds'], 0)
        self.assertTrue(failed['recommended_action'])
        self.assertNotIn('smoke_checks', stages)
        self.assertTrue(report['blocking_issues'])
        self.assertFalse(report['production_switched'])

    def test_a_report_is_written_on_success_and_on_failure(self):
        for fail_at in (None, 'export_bundle'):
            with self.subTest(fail_at=fail_at):
                self.json_report.unlink(missing_ok=True)
                self.markdown_report.unlink(missing_ok=True)
                if self.bundle.exists():
                    shutil.rmtree(self.bundle)
                if self.target_media.exists():
                    shutil.rmtree(self.target_media)

                self.run_main(FakeRunner(fail_at=fail_at))

                self.assertTrue(self.json_report.is_file())
                self.assertTrue(self.markdown_report.is_file())
                report = orchestrator.read_json(self.json_report)
                self.assertIn(report['status'], {'ok', 'failed'})

    def test_the_diagnostic_mode_propagates_the_missing_media_flags(self):
        runner = FakeRunner()

        self.run_main(runner, '--diagnostic-missing-media')

        arguments = {call['name']: call['arguments'] for call in runner.calls}
        self.assertIn('--allow-missing-media', arguments['export_bundle'])
        self.assertIn('--accept-missing-media', arguments['import_bundle'])
        self.assertIn('--allow-missing-media', arguments['verify_bundle'])


class InputValidationTests(OrchestratorTestCase):
    def test_the_working_database_is_refused_without_the_explicit_flag(self):
        runner = FakeRunner()
        working = Path(settings.BASE_DIR) / 'db.sqlite3'
        argv = self.argv()
        argv[argv.index('--source-db') + 1] = str(working)

        exit_code = orchestrator.main(
            argv, runner=runner, environ=self.environment, stream=StringIO()
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(runner.calls, [])
        report = orchestrator.read_json(self.json_report)
        self.assertTrue(any('db.sqlite3' in issue for issue in report['blocking_issues']))

    def test_missing_postgresql_environment_stops_before_any_subprocess(self):
        runner = FakeRunner()

        exit_code = self.run_main(runner, environment={'DB_NAME': 'only-a-name'})

        self.assertEqual(exit_code, 2)
        self.assertEqual(runner.calls, [])
        report = orchestrator.read_json(self.json_report)
        joined = ' '.join(report['blocking_issues'])
        self.assertIn('DB_USER', joined)
        self.assertIn('DB_PASSWORD', joined)

    def test_a_non_empty_bundle_directory_is_refused(self):
        self.bundle.mkdir()
        (self.bundle / 'leftover.txt').write_text('x', encoding='utf-8')
        runner = FakeRunner()

        exit_code = self.run_main(runner)

        self.assertEqual(exit_code, 2)
        self.assertEqual(runner.calls, [])

    def test_a_missing_media_copy_is_refused(self):
        shutil.rmtree(self.source_media)
        runner = FakeRunner()

        exit_code = self.run_main(runner)

        self.assertEqual(exit_code, 2)
        self.assertEqual(runner.calls, [])


class SecretRedactionTests(OrchestratorTestCase):
    def test_no_report_contains_a_known_secret_value(self):
        leaking_output = (
            f'connection failed: password={SECRET_PASSWORD} key={SECRET_KEY_VALUE}'
        )
        runner = FakeRunner(fail_at='target_preflight', stdout=leaking_output, stderr=leaking_output)

        self.run_main(runner)

        json_text = self.json_report.read_text(encoding='utf-8')
        markdown_text = self.markdown_report.read_text(encoding='utf-8')
        for secret in (SECRET_PASSWORD, SECRET_KEY_VALUE):
            self.assertNotIn(secret, json_text)
            self.assertNotIn(secret, markdown_text)
        self.assertIn(orchestrator.REDACTED, json_text)

    def test_the_password_is_never_passed_as_a_command_line_argument(self):
        runner = FakeRunner()

        self.run_main(runner)

        for call in runner.calls:
            self.assertNotIn(SECRET_PASSWORD, ' '.join(call['arguments']))
            # It still reaches the subprocess the only correct way: the environment.
            self.assertEqual(call['env']['DB_PASSWORD'], SECRET_PASSWORD)

    def test_reports_do_not_expose_absolute_server_paths(self):
        runner = FakeRunner(
            fail_at='import_bundle',
            stdout=f'Пакет прочитан из {self.bundle.resolve()}',
            stderr=f'Не удалось открыть {self.target_media.resolve()}',
        )

        self.run_main(runner)

        json_text = self.json_report.read_text(encoding='utf-8')
        markdown_text = self.markdown_report.read_text(encoding='utf-8')
        for absolute in (
            str(self.work_dir),
            str(self.source_media.resolve()),
            str(self.bundle.resolve()),
            str(self.target_media.resolve()),
        ):
            self.assertNotIn(absolute.replace('\\', '\\\\'), json_text)
            self.assertNotIn(absolute, markdown_text)

    def test_path_redaction_shortens_paths_without_touching_prose(self):
        self.assertEqual(
            orchestrator.redact_paths(r'вывод: C:\Users\Ivan\transfer\bundle'),
            'вывод: .../transfer/bundle',
        )
        self.assertEqual(
            orchestrator.redact_paths('вывод: /srv/app/transfer/report.json'),
            'вывод: .../transfer/report.json',
        )
        self.assertEqual(
            orchestrator.redact_paths('Разрешить использование и/или доработку'),
            'Разрешить использование и/или доработку',
        )
        self.assertEqual(
            orchestrator.redact_paths('Файл базы: db-copy.sqlite3, 483328 байт.'),
            'Файл базы: db-copy.sqlite3, 483328 байт.',
        )


class MarkdownReportTests(OrchestratorTestCase):
    def test_the_markdown_report_covers_every_required_section(self):
        self.run_main(FakeRunner())

        text = self.markdown_report.read_text(encoding='utf-8')
        for heading in (
            '# Отчёт репетиции переноса SQLite → PostgreSQL',
            '## Запуск и окружение',
            '## Источник',
            '## Пакет',
            '## Этапы',
            '## Проверка источника',
            '## Проверка целевой базы',
            '## Импорт',
            '## Итоговая сверка',
            '## Smoke-проверки',
            '## Отсутствующие файлы вложений',
            '## Предупреждения',
            '## Оценка окна простоя',
            '## Блокирующие проблемы для production-переезда',
        ):
            self.assertIn(heading, text)
        self.assertIn('Рабочая система на PostgreSQL не переключена', text)

    def test_the_json_report_records_environment_and_downtime_estimate(self):
        self.run_main(FakeRunner())

        report = orchestrator.read_json(self.json_report)
        versions = report['environment']['versions']
        for key in ('python', 'django', 'psycopg', 'sqlite', 'postgresql'):
            self.assertIn(key, versions)
        self.assertIn('os', report['environment'])
        self.assertIn('git_commit', report['environment'])
        self.assertEqual(report['source']['sqlite_file'], self.source_db.name)
        self.assertEqual(report['source']['media_file_count'], 1)
        self.assertGreater(report['downtime_estimate']['estimated_window_seconds'], 0)
        self.assertIn('measured_seconds', report['downtime_estimate'])
