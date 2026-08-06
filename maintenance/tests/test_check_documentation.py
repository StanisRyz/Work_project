"""Tests for the read-only `check_documentation` command.

Every test builds a throwaway documentation tree in a temporary directory and
points the command at it with `--root`, so nothing here depends on — or can
disturb — the project's real documents.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase

from maintenance.management.commands.check_documentation import (
    BLOCKING,
    run_documentation_checks,
)


ARCHIVE_BANNER = '> **Внимание: исторический документ.**\n'


class DocumentationTreeMixin:
    """Builds a minimal, valid documentation tree that individual tests bend."""

    def setUp(self):
        super().setUp()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        (self.root / 'docs' / 'archive').mkdir(parents=True)
        self.write('README.md', '# Проект\n\nСм. [карту](docs/index.md).\n')
        self.write('AGENTS.md', '# Agent Notes\n\nSee [domain](docs/domain.md).\n')
        self.write(
            'docs/index.md',
            '# Карта\n\n- [Предметная область](domain.md)\n'
            '- [Архив](archive/README.md)\n',
        )
        self.write('docs/domain.md', '# Предметная область\n\nРоли и статусы.\n')
        self.write(
            'docs/archive/README.md',
            f'# Архив\n\n{ARCHIVE_BANNER}\nИсторические материалы.\n',
        )

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def append(self, relative, text):
        path = self.root / relative
        path.write_text(path.read_text(encoding='utf-8') + text, encoding='utf-8')

    def run_checks(self):
        return run_documentation_checks(self.root)

    def failures(self, check=None):
        return [
            result
            for result in self.run_checks()
            if result['status'] == BLOCKING and (check is None or result['check'] == check)
        ]


class CheckDocumentationTests(DocumentationTreeMixin, SimpleTestCase):
    def test_valid_documentation_set_passes(self):
        self.assertEqual(self.failures(), [])

    def test_broken_relative_link_is_reported(self):
        self.append('docs/domain.md', '\nСм. [удалённый документ](removed.md).\n')

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['links'])
        self.assertIn('removed.md', failures[0]['detail'])

    def test_broken_link_in_entry_document_is_reported_separately(self):
        # README / AGENTS / docs/index.md are where a reader starts, so a
        # dangling link there is reported under its own check name.
        self.append('README.md', '\n[Развёртывание](docs/deployment.md)\n')

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['entry_links'])

    def test_external_url_is_ignored(self):
        self.append(
            'docs/domain.md',
            '\n[Django](https://docs.djangoproject.com/en/6.0/) и '
            '[почта](mailto:quality@example.internal).\n',
        )

        self.assertEqual(self.failures(), [])

    def test_anchor_links_are_handled(self):
        # A pure anchor resolves inside the document; an anchor on a real file
        # must be stripped before the path is checked.
        self.append(
            'docs/index.md',
            '\n[к разделу](#карта) и [к разделу другого документа](domain.md#роли)\n',
        )

        self.assertEqual(self.failures(), [])

    def test_anchor_on_missing_file_is_still_reported(self):
        self.append('docs/index.md', '\n[нет такого](missing.md#раздел)\n')

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['entry_links'])
        self.assertIn('missing.md#раздел', failures[0]['detail'])

    def test_stage_marker_in_active_document_is_reported(self):
        self.append('docs/domain.md', '\nЭтап RT-3 добавил сверку ревизий.\n')

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['stage_markers'])
        self.assertIn('RT-3', failures[0]['detail'])

    def test_stage_markers_are_allowed_in_archive(self):
        self.write(
            'docs/archive/rehearsal.md',
            f'# Репетиция\n\n{ARCHIVE_BANNER}\nЭтапы D11, RT-3, STAB-2 и OBS-1.\n',
        )

        self.assertEqual(self.failures(), [])

    def test_archive_document_without_notice_is_reported(self):
        self.write('docs/archive/rehearsal.md', '# Репетиция\n\nБез предупреждения.\n')

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['archive_notice'])

    def test_missing_management_command_is_reported(self):
        self.append(
            'docs/domain.md',
            '\n```powershell\npython manage.py rebuild_everything\n```\n',
        )

        failures = self.failures()

        self.assertEqual([item['check'] for item in failures], ['commands'])
        self.assertIn('rebuild_everything', failures[0]['detail'])

    def test_existing_management_command_passes(self):
        self.append(
            'docs/domain.md',
            '\n```powershell\npython manage.py seed_references\n```\n',
        )

        self.assertEqual(self.failures(), [])

    def test_empty_active_document_is_reported(self):
        self.write('docs/development.md', '\n   \n')

        failures = self.failures('structure')

        self.assertIn('пуст', failures[0]['detail'])

    def test_document_without_single_h1_is_reported(self):
        self.write('docs/development.md', '# Первый\n\n# Второй\n\nТекст.\n')

        failures = self.failures('structure')

        self.assertIn('H1', failures[0]['detail'])

    def test_headings_inside_code_fences_are_not_counted(self):
        self.append(
            'docs/domain.md',
            '\n```bash\n# комментарий, а не заголовок\necho ok\n```\n',
        )

        self.assertEqual(self.failures(), [])

    def test_command_reports_failure_with_non_zero_exit(self):
        self.append('docs/domain.md', '\n[нет такого](removed.md)\n')

        with self.assertRaises(SystemExit):
            call_command('check_documentation', root=str(self.root), stdout=StringIO())

    def test_command_succeeds_on_a_valid_tree(self):
        out = StringIO()

        call_command('check_documentation', root=str(self.root), stdout=out)

        self.assertIn('Документация корректна', out.getvalue())

    def test_command_changes_nothing(self):
        before = {
            path.relative_to(self.root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }

        call_command('check_documentation', root=str(self.root), stdout=StringIO())

        after = {
            path.relative_to(self.root).as_posix(): (
                path.read_bytes(),
                path.stat().st_mtime_ns,
            )
            for path in sorted(self.root.rglob('*'))
            if path.is_file()
        }
        self.assertEqual(before, after)


class ProjectDocumentationTests(SimpleTestCase):
    """The project's own documents must satisfy every blocking rule."""

    def test_repository_documentation_passes(self):
        from django.conf import settings

        failures = [
            result
            for result in run_documentation_checks(Path(settings.BASE_DIR))
            if result['status'] == BLOCKING
        ]

        self.assertEqual(failures, [], msg=str(failures))
