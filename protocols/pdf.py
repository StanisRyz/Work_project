"""The protocol document as a PDF file.

Two rules shape this module:

* **No business logic.** It renders `selectors.build_protocol_document()` and
  nothing else — the same structure the printable HTML page renders. Numbering,
  approval reasons, generated tasks and the workflow state are read from what
  the workflow already stored; none of it is recomputed here.
* **Pure Python, no native libraries.** The application is deployed on both
  Linux and Windows, so a renderer needing GTK/Pango is not an option.
  ReportLab is a wheel with no system dependencies.

Cyrillic needs a real TrueType font: ReportLab's built-in Type1 faces are
Latin-only, and the fonts it ships (Bitstream Vera) have no Cyrillic block.
The font is therefore resolved from configuration first and from the usual
Linux/Windows locations second, and a missing one is a controlled refusal —
never a PDF full of black squares. `ecosystem.checks` turns the same
resolution into a deployment check, so this is caught before a user clicks.
"""

import io
from pathlib import Path

from django.conf import settings


class ProtocolPdfUnavailable(RuntimeError):
    """PDF generation is not configured on this installation."""


# Fonts that actually contain Cyrillic, in the places distributions put them.
# Order matters only in that the first readable file wins.
FONT_CANDIDATES = (
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
     '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    ('C:/Windows/Fonts/calibri.ttf', 'C:/Windows/Fonts/calibrib.ttf'),
)

REGULAR_FONT = 'ProtocolBody'
BOLD_FONT = 'ProtocolBodyBold'

_fonts_registered = False


def _readable(path):
    if not path:
        return None
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def resolve_fonts():
    """Return `(regular_path, bold_path)` for a Cyrillic-capable face.

    The configured pair wins outright — an installation that ships its own
    corporate font must not be second-guessed. Bold is optional everywhere: a
    document set entirely in the regular face is ugly, not wrong, so a missing
    bold file falls back to the regular one instead of refusing.
    """
    configured = _readable(getattr(settings, 'PROTOCOL_PDF_FONT_PATH', ''))
    if configured is not None:
        bold = _readable(getattr(settings, 'PROTOCOL_PDF_FONT_BOLD_PATH', ''))
        return configured, bold or configured

    for regular, bold in FONT_CANDIDATES:
        found = _readable(regular)
        if found is not None:
            return found, _readable(bold) or found
    return None, None


def describe_availability():
    """`(is_available, reason)` — used by the view and by the deployment check."""
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False, 'Библиотека reportlab не установлена.'
    regular, _bold = resolve_fonts()
    if regular is None:
        return False, 'Не найден шрифт с кириллицей для PDF.'
    return True, ''


def _register_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    regular, bold = resolve_fonts()
    if regular is None:
        raise ProtocolPdfUnavailable(
            'Не найден шрифт с кириллицей. Укажите PROTOCOL_PDF_FONT_PATH.'
        )
    pdfmetrics.registerFont(TTFont(REGULAR_FONT, str(regular)))
    pdfmetrics.registerFont(TTFont(BOLD_FONT, str(bold)))
    _fonts_registered = True


def _styles():
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle(
        'ProtocolBody', fontName=REGULAR_FONT, fontSize=9, leading=12
    )
    return {
        'title': ParagraphStyle(
            'ProtocolTitle', parent=body, fontName=BOLD_FONT, fontSize=16, leading=20,
            spaceAfter=2,
        ),
        'eyebrow': ParagraphStyle(
            'ProtocolEyebrow', parent=body, fontSize=9, textColor='#667085', spaceAfter=10,
        ),
        'heading': ParagraphStyle(
            'ProtocolHeading', parent=body, fontName=BOLD_FONT, fontSize=12, leading=15,
            spaceBefore=14, spaceAfter=6,
        ),
        'body': body,
        'cell': ParagraphStyle('ProtocolCell', parent=body),
        'header_cell': ParagraphStyle('ProtocolHeaderCell', parent=body, fontName=BOLD_FONT),
        'empty': ParagraphStyle('ProtocolEmpty', parent=body, textColor='#667085'),
    }


def _date(value, fmt='%d.%m.%Y %H:%M'):
    if not value:
        return '—'
    from django.utils import timezone as django_timezone

    if hasattr(value, 'tzinfo') and value.tzinfo is not None:
        value = django_timezone.localtime(value)
    return value.strftime(fmt)


def _table(styles, header, rows, widths):
    """One bordered table in the same visual language as the printable page."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    data = [[Paragraph(str(cell), styles['header_cell']) for cell in header]]
    data += [[Paragraph(str(cell), styles['cell']) for cell in row] for row in rows]
    table = Table(data, colWidths=widths, repeatRows=1, hAlign='LEFT')
    table.setStyle(
        TableStyle(
            [
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#d9dee8')),
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#f6f7f9')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _section(story, styles, heading, table_or_none, empty_message):
    from reportlab.platypus import Paragraph

    story.append(Paragraph(heading, styles['heading']))
    if table_or_none is None:
        story.append(Paragraph(empty_message, styles['empty']))
    else:
        story.append(table_or_none)


def render_protocol_pdf(document):
    """Return the document as PDF bytes.

    Raises `ProtocolPdfUnavailable` when the installation cannot produce one,
    so the caller answers with a clear message instead of a broken file.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
    except ImportError as exc:  # pragma: no cover - exercised through the view
        raise ProtocolPdfUnavailable('Библиотека reportlab не установлена.') from exc

    _register_fonts()
    styles = _styles()

    buffer = io.BytesIO()
    page = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=document['title'],
        author=document['author_name'],
    )
    width = page.width

    story = [
        Paragraph('Протокол совещания', styles['eyebrow']),
        Paragraph(document['title'], styles['title']),
        Spacer(1, 6),
    ]

    _section(
        story, styles, 'Данные протокола',
        _table(
            styles,
            ['Поле', 'Значение'],
            [
                ['Тип протокола', document['protocol_type_name']],
                ['Номер', f"№{document['number']}"],
                ['Дата создания', _date(document['created_at'])],
                ['Автор', document['author_name']],
                ['Статус', document['status_label']],
                ['Редакция', document['revision'] or '—'],
            ],
            [width * 0.28, width * 0.72],
        ),
        '',
    )

    _section(
        story, styles, 'Участники',
        _table(
            styles,
            ['№', 'Подразделение', 'Сотрудник', 'Должность', 'Согласование'],
            [
                [
                    index,
                    participant['department_name'] or '—',
                    participant['display_name']
                    + (' (автор)' if participant['is_author'] else ''),
                    participant['position'] or '—',
                    'Требуется' if participant['requires_approval'] else '—',
                ]
                for index, participant in enumerate(document['participants'], start=1)
            ],
            [width * 0.06, width * 0.22, width * 0.3, width * 0.26, width * 0.16],
        ) if document['participants'] else None,
        'Участники не добавлены.',
    )

    _section(
        story, styles, 'Повестка',
        _table(
            styles,
            ['№', 'Вопрос'],
            [[index, text] for index, text in enumerate(document['agenda'], start=1)],
            [width * 0.06, width * 0.94],
        ) if document['agenda'] else None,
        'Вопросы повестки не добавлены.',
    )

    _section(
        story, styles, 'Слушали',
        _table(
            styles,
            ['Выступающий', 'Содержание'],
            [[speech['speaker'], speech['text']] for speech in document['speeches']],
            [width * 0.28, width * 0.72],
        ) if document['speeches'] else None,
        'Выступления не добавлены.',
    )

    _section(
        story, styles, 'Решили',
        _table(
            styles,
            ['№', 'Решение', 'Подразделение', 'Исполнители', 'Срок'],
            [
                [
                    index,
                    decision['text'],
                    decision['department_name'] or '—',
                    ', '.join(decision['assignees']) or '—',
                    _date(decision['due_date'], '%d.%m.%Y'),
                ]
                for index, decision in enumerate(document['decisions'], start=1)
            ],
            [width * 0.06, width * 0.36, width * 0.18, width * 0.26, width * 0.14],
        ) if document['decisions'] else None,
        'Решения не приняты.',
    )

    if document['tasks']:
        _section(
            story, styles, 'Созданные задачи',
            _table(
                styles,
                ['№ задачи', 'Задача', 'Подразделение', 'Исполнители', 'Срок', 'Статус'],
                [
                    [
                        task['id'],
                        task['text'],
                        task['department_name'] or '—',
                        ', '.join(task['assignees']) or '—',
                        _date(task['due_date'], '%d.%m.%Y'),
                        task['status_label'],
                    ]
                    for task in document['tasks']
                ],
                [
                    width * 0.09, width * 0.29, width * 0.16,
                    width * 0.2, width * 0.12, width * 0.14,
                ],
            ),
            '',
        )

    progress = document['approval_progress']
    if document['approvals']:
        _section(
            story, styles, 'Согласование',
            _table(
                styles,
                ['Согласующий', 'Подразделение', 'Основание', 'Решение', 'Дата и время'],
                [
                    [
                        approval['display_name']
                        + (f", {approval['position']}" if approval['position'] else ''),
                        approval['department_name'] or '—',
                        approval['reason'] or '—',
                        approval['status_label']
                        + (
                            f" — {approval['return_comment']}"
                            if approval['return_comment']
                            else ''
                        ),
                        _date(approval['decided_at']),
                    ]
                    for approval in document['approvals']
                ],
                [width * 0.26, width * 0.16, width * 0.22, width * 0.22, width * 0.14],
            ),
            '',
        )
        story.append(Spacer(1, 6))
        story.append(
            _table(
                styles,
                ['Итог', 'Значение'],
                [
                    [
                        'Согласовано',
                        f"{progress['approved']} из {progress['total']}, "
                        f"редакция {progress['revision']}",
                    ],
                    ['Состояние протокола', document['status_label']],
                ],
                [width * 0.28, width * 0.72],
            )
        )
    else:
        _section(
            story, styles, 'Согласование', None,
            f"Протокол не требовал согласования. Состояние: {document['status_label']}.",
        )

    _section(
        story, styles, 'История',
        _table(
            styles,
            ['Дата', 'Событие', 'Пользователь', 'Комментарий'],
            [
                [
                    _date(event['created_at']),
                    event['event_label'],
                    event['actor_name'],
                    event['message'],
                ]
                for event in document['history']
            ],
            [width * 0.15, width * 0.24, width * 0.21, width * 0.4],
        ) if document['history'] else None,
        'История протокола пока пуста.',
    )

    page.build(story)
    return buffer.getvalue()


def protocol_pdf_filename(document):
    """A safe ASCII-free-of-surprises file name: type code, number, revision."""
    code = document['protocol'].protocol_type.code.lower()
    return f"protocol-{code}-{document['number']}.pdf"
