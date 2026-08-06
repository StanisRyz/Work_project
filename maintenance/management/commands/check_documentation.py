"""Verify that the documentation set is internally consistent.

Read-only: it parses Markdown as plain text and writes nothing anywhere. No
third-party Markdown library, linter or documentation generator is involved —
only the small subset of syntax this project actually uses.

What it enforces:

* every relative Markdown link resolves to an existing file or directory;
* active documents carry no historical stage markers (D11, RT-3, STAB-2,
  OBS-1 and the like) — those are allowed only under `docs/archive/`;
* every archived document opens with a visible «historical» warning;
* every management command mentioned in an active document really exists;
* no active document is empty, and each has exactly one H1.

Line counts are advisory: a long document is a warning, never a failure.
"""

import re
from pathlib import Path

from django.conf import settings
from django.core.management import get_commands
from django.core.management.base import BaseCommand


PASS = 'ok'
WARNING = 'warning'
BLOCKING = 'blocking'

_LABELS = {PASS: 'PASS', WARNING: 'WARNING', BLOCKING: 'BLOCKING'}


# Documents outside `docs/` that are part of the active set.
ROOT_DOCUMENTS = ('README.md', 'AGENTS.md')
DOCS_DIRECTORY = 'docs'
ARCHIVE_DIRECTORY = 'docs/archive'

# The entry points a reader starts from. A dangling link here is the most
# damaging kind, so it is reported separately from the generic link check.
ENTRY_DOCUMENTS = ('README.md', 'AGENTS.md', 'docs/index.md')

# Advisory size targets. Deliberately not blocking: a document that genuinely
# needs the space must not fail a check. The tolerance exists because the rule
# is «noticeably longer than the target», not «one line over it» — an exact
# line count is a bad thing to write documentation against.
LINE_WARNING_LIMITS = {'README.md': 220, 'AGENTS.md': 190}
DEFAULT_LINE_WARNING_LIMIT = 350
LINE_WARNING_TOLERANCE = 1.15

# An archived document must say so near the top, in words a reader sees before
# acting on anything it contains.
ARCHIVE_NOTICE_LINES = 20
ARCHIVE_NOTICE_PATTERN = re.compile(r'историч', re.IGNORECASE)

# Stage markers from the project's own history. They describe *when* something
# was built, which an active document must never depend on.
STAGE_MARKER_PATTERN = re.compile(
    r'(?<![\w-])(?:D\d{1,3}|RT-\d{1,3}|STAB-\d{1,3}|OBS-\d{1,3})(?![\w-])'
)

# `[text](target)`, optionally followed by a quoted title.
LINK_PATTERN = re.compile(r'\[[^\]]*\]\(\s*<?([^)>\s]+)>?(?:\s+"[^"]*")?\s*\)')

# Fenced code blocks: their contents are examples, not prose.
FENCE_PATTERN = re.compile(r'^\s*(```|~~~)')

# `python manage.py <command>` in any of its written forms.
COMMAND_PATTERN = re.compile(r'manage\.py\s+([a-z][a-z0-9_]*)')

# Links that point outside the repository and cannot be resolved locally.
EXTERNAL_SCHEMES = ('http://', 'https://', 'mailto:', 'ftp://', 'tel:')


class Command(BaseCommand):
    help = (
        'Проверяет документацию: относительные ссылки, отсутствие исторических '
        'маркеров этапов в активных документах, предупреждение в архивных, '
        'существование упомянутых management-команд, непустые документы и один '
        'заголовок H1. Только чтение.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--root',
            help='Каталог проекта для проверки (по умолчанию BASE_DIR).',
        )

    def handle(self, *args, **options):
        root = Path(options.get('root') or settings.BASE_DIR)
        results = run_documentation_checks(root)

        self.stdout.write('Проверка документации')
        self.stdout.write('')
        for item in results:
            self.stdout.write(f'  [{_LABELS[item["status"]]:<8}] {item["check"]}: {item["detail"]}')

        blocking = [item for item in results if item['status'] == BLOCKING]
        warnings = [item for item in results if item['status'] == WARNING]
        self.stdout.write('')
        if blocking:
            self.stdout.write(
                self.style.ERROR(f'Документация не прошла проверку: проблем — {len(blocking)}.')
            )
            raise SystemExit(1)
        if warnings:
            self.stdout.write(
                self.style.WARNING(f'Документация корректна, предупреждений — {len(warnings)}.')
            )
        else:
            self.stdout.write(self.style.SUCCESS('Документация корректна.'))
        return None


def _result(check, status, detail):
    return {'check': check, 'status': status, 'detail': detail}


def _relative(root, path):
    """Repository-relative POSIX path, used in every message."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def collect_documents(root):
    """Return `(active, archived)` lists of Markdown files, sorted."""
    active = []
    for name in ROOT_DOCUMENTS:
        candidate = root / name
        if candidate.is_file():
            active.append(candidate)

    docs = root / DOCS_DIRECTORY
    archive = root / ARCHIVE_DIRECTORY
    archived = []
    if docs.is_dir():
        for path in sorted(docs.rglob('*.md')):
            if archive in path.parents or path.parent == archive:
                archived.append(path)
            else:
                active.append(path)
    return sorted(set(active)), sorted(set(archived))


def strip_code_fences(text):
    """Return the prose lines only, with fenced code blocks removed.

    A code sample may legitimately contain a `#` heading or a bracketed
    expression that is not a Markdown link, so content checks look at prose.
    """
    lines = []
    inside = False
    for line in text.split('\n'):
        if FENCE_PATTERN.match(line):
            inside = not inside
            continue
        if not inside:
            lines.append(line)
    return lines


def _check_links(root, documents):
    """Every relative link must resolve. Anchors and external URLs are skipped."""
    results = []
    for path in documents:
        prose = '\n'.join(strip_code_fences(path.read_text(encoding='utf-8')))
        broken = []
        for target in LINK_PATTERN.findall(prose):
            if target.startswith('#'):
                # An in-document anchor: nothing to resolve on disk.
                continue
            if target.startswith(EXTERNAL_SCHEMES) or target.startswith('//'):
                continue
            # `file.md#section` — the anchor is not part of the path.
            local = target.split('#', 1)[0].split('?', 1)[0]
            if not local:
                continue
            resolved = (path.parent / local).resolve()
            if not resolved.exists():
                broken.append(target)
        name = _relative(root, path)
        if broken:
            entry = name in ENTRY_DOCUMENTS
            results.append(
                _result(
                    'entry_links' if entry else 'links',
                    BLOCKING,
                    f'{name}: ссылки ведут на отсутствующие файлы — {", ".join(sorted(broken))}.',
                )
            )
    if not results:
        results.append(
            _result('links', PASS, f'Все относительные ссылки разрешаются ({len(documents)} документов).')
        )
    return results


def _check_stage_markers(root, documents):
    """Historical stage markers are allowed only under `docs/archive/`."""
    results = []
    for path in documents:
        prose = '\n'.join(strip_code_fences(path.read_text(encoding='utf-8')))
        markers = sorted(set(STAGE_MARKER_PATTERN.findall(prose)))
        if markers:
            results.append(
                _result(
                    'stage_markers',
                    BLOCKING,
                    f'{_relative(root, path)}: исторические маркеры этапов — '
                    f'{", ".join(markers)}. Они допустимы только в {ARCHIVE_DIRECTORY}/.',
                )
            )
    if not results:
        results.append(
            _result('stage_markers', PASS, 'Активные документы не содержат маркеров этапов.')
        )
    return results


def _check_archive_notices(root, archived):
    """Every archived document must warn that it is historical."""
    if not archived:
        return [_result('archive_notice', PASS, 'Архивных документов нет.')]
    results = []
    for path in archived:
        head = path.read_text(encoding='utf-8').split('\n')[:ARCHIVE_NOTICE_LINES]
        if not ARCHIVE_NOTICE_PATTERN.search('\n'.join(head)):
            results.append(
                _result(
                    'archive_notice',
                    BLOCKING,
                    f'{_relative(root, path)}: нет предупреждения об историческом статусе '
                    f'в первых {ARCHIVE_NOTICE_LINES} строках.',
                )
            )
    if not results:
        results.append(
            _result('archive_notice', PASS, f'Все архивные документы ({len(archived)}) помечены.')
        )
    return results


def _check_commands(root, documents):
    """Every `manage.py <command>` mentioned in an active document must exist."""
    known = set(get_commands())
    missing = {}
    for path in documents:
        # Raw text on purpose: commands are almost always inside a code block.
        for name in COMMAND_PATTERN.findall(path.read_text(encoding='utf-8')):
            if name not in known:
                missing.setdefault(name, set()).add(_relative(root, path))
    if missing:
        return [
            _result(
                'commands',
                BLOCKING,
                f'{name}: команда не зарегистрирована (упомянута в '
                f'{", ".join(sorted(sources))}).',
            )
            for name, sources in sorted(missing.items())
        ]
    return [_result('commands', PASS, 'Все упомянутые management-команды существуют.')]


def _check_structure(root, documents):
    """No empty active document, and exactly one H1 per document."""
    results = []
    for path in documents:
        text = path.read_text(encoding='utf-8')
        name = _relative(root, path)
        if not text.strip():
            results.append(_result('structure', BLOCKING, f'{name}: документ пуст.'))
            continue
        headings = [line for line in strip_code_fences(text) if line.startswith('# ')]
        if len(headings) != 1:
            results.append(
                _result(
                    'structure',
                    BLOCKING,
                    f'{name}: заголовков H1 — {len(headings)}, должен быть ровно один.',
                )
            )
    if not results:
        results.append(
            _result('structure', PASS, f'Все активные документы непусты и имеют один H1.')
        )
    return results


def _check_length(root, documents):
    """Advisory only: a noticeably long document is a warning, never a failure."""
    results = []
    for path in documents:
        name = _relative(root, path)
        limit = LINE_WARNING_LIMITS.get(name, DEFAULT_LINE_WARNING_LIMIT)
        count = len(path.read_text(encoding='utf-8').split('\n'))
        if count > limit * LINE_WARNING_TOLERANCE:
            results.append(
                _result(
                    'length',
                    WARNING,
                    f'{name}: {count} строк заметно превышает ориентир {limit}. '
                    'Допустимо, если содержимое уникально.',
                )
            )
    if not results:
        results.append(_result('length', PASS, 'Объём документов в пределах ориентиров.'))
    return results


def run_documentation_checks(root):
    """Run every documentation check and return the flat result list."""
    root = Path(root)
    active, archived = collect_documents(root)
    if not active:
        return [_result('documents', BLOCKING, 'Не найдено ни одного активного документа.')]

    results = [
        _result(
            'documents',
            PASS,
            f'Активных документов — {len(active)}, архивных — {len(archived)}.',
        )
    ]
    results += _check_links(root, active + archived)
    results += _check_stage_markers(root, active)
    results += _check_archive_notices(root, archived)
    results += _check_commands(root, active)
    results += _check_structure(root, active)
    results += _check_length(root, active)
    return results
