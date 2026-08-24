"""The protocol document as a PDF file.

Two rules shape this module:

* **No business logic.** It renders `selectors.build_protocol_document()` and
  nothing else — the same structure the printable HTML page renders. Numbering,
  approval reasons, generated tasks and the workflow state are read from what
  the workflow already stored; none of it is recomputed here.
* **Pure Python, no native libraries.** The application is deployed on both
  Linux and Windows, so a renderer needing GTK/Pango is not an option.
  ReportLab is a wheel with no system dependencies.

The layout is the plant's existing paper protocol: a centred «ПРОТОКОЛ», the
date and number on one line, Присутствовали, Повестка, Слушали, Решили,
signature lines and «Подготовил». Flowing serif text on white — no tables of
fields, no borders, no badges.

Cyrillic needs a real TrueType font: ReportLab's built-in Type1 faces are
Latin-only, and the fonts it ships (Bitstream Vera) have no Cyrillic block.
A serif face is preferred because the document is a serif document; the font is
resolved from configuration first and from the usual Linux/Windows locations
second, and a missing one is a controlled refusal — never a PDF full of black
squares. `ecosystem.checks` turns the same resolution into a deployment check,
so this is caught before a user clicks.
"""

import io
from pathlib import Path

from django.conf import settings


class ProtocolPdfUnavailable(RuntimeError):
    """PDF generation is not configured on this installation."""


# Fonts that actually contain Cyrillic, in the places distributions put them.
# Serif first — the document is a serif document — then the sans faces, which
# are only a fallback so a machine without a serif face still produces a file.
FONT_CANDIDATES = (
    ('C:/Windows/Fonts/times.ttf', 'C:/Windows/Fonts/timesbd.ttf'),
    ('/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf',
     '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf'),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf'),
    ('/usr/share/fonts/dejavu/DejaVuSerif.ttf',
     '/usr/share/fonts/dejavu/DejaVuSerif-Bold.ttf'),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
     '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
)

REGULAR_FONT = 'ProtocolBody'
BOLD_FONT = 'ProtocolBodyBold'

MONTHS = (
    'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
    'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
)

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
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
    from reportlab.lib.styles import ParagraphStyle

    body = ParagraphStyle('Body', fontName=REGULAR_FONT, fontSize=11, leading=14)
    return {
        'body': body,
        'title': ParagraphStyle(
            'Title', parent=body, fontName=BOLD_FONT, fontSize=13, leading=16,
            alignment=TA_CENTER, spaceAfter=14,
        ),
        'heading': ParagraphStyle(
            'Heading', parent=body, fontName=BOLD_FONT, spaceBefore=12, spaceAfter=4,
        ),
        'person': ParagraphStyle('Person', parent=body, spaceAfter=1),
        'speaker': ParagraphStyle('Speaker', parent=body, firstLineIndent=18, spaceBefore=6),
        'speech': ParagraphStyle(
            'Speech', parent=body, firstLineIndent=18, alignment=TA_JUSTIFY, spaceAfter=4,
        ),
        # Numbered items: the number is drawn in the gutter by `bulletText`.
        'item': ParagraphStyle(
            'Item', parent=body, leftIndent=24, bulletIndent=6, spaceAfter=3,
        ),
        'fact': ParagraphStyle('Fact', parent=body, leftIndent=24, spaceAfter=2),
        'prepared': ParagraphStyle('Prepared', parent=body, fontSize=9, leading=12),
    }


def _localdate(value):
    if not value:
        return None
    from django.utils import timezone as django_timezone

    if hasattr(value, 'tzinfo') and value.tzinfo is not None:
        return django_timezone.localtime(value).date()
    return value


def _long_date(value):
    """«22 июля 2026» — the form the paper protocol uses in its header."""
    moment = _localdate(value)
    if moment is None:
        return ''
    return f'{moment.day} {MONTHS[moment.month - 1]} {moment.year}'


def _short_date(value):
    moment = _localdate(value)
    return moment.strftime('%d.%m.%Y') if moment else '—'


def _escape(text):
    """ReportLab paragraphs are mini-HTML, so business text must be escaped.

    Line breaks are kept as `<br/>`: a decision or a speech written as several
    lines has to print as several lines.
    """
    from xml.sax.saxutils import escape

    return escape(str(text or '')).replace('\n', '<br/>')


def render_protocol_pdf(document):
    """Return the document as PDF bytes.

    Raises `ProtocolPdfUnavailable` when the installation cannot produce one,
    so the caller answers with a clear message instead of a broken file.
    """
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - exercised through the view
        raise ProtocolPdfUnavailable('Библиотека reportlab не установлена.') from exc

    _register_fonts()
    styles = _styles()

    buffer = io.BytesIO()
    page = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=document['title'],
        author=document['author_name'],
    )
    width = page.width

    story = [Paragraph('Протокол', styles['title'])]

    # Date left, «№ 14 / Качество» right, on one line. A borderless two-column
    # table is the only way ReportLab aligns both edges of a single line.
    header = Table(
        [[
            Paragraph(_long_date(document['created_at']), styles['body']),
            Paragraph(
                f"№ {document['number']} / {_escape(document['protocol_type_name'])}",
                styles['body'],
            ),
        ]],
        colWidths=[width * 0.5, width * 0.5],
        hAlign='LEFT',
    )
    header.setStyle(
        TableStyle(
            [
                ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.append(header)

    # -- Присутствовали
    story.append(Paragraph('Присутствовали:', styles['heading']))
    if document['participants']:
        last = len(document['participants']) - 1
        for index, participant in enumerate(document['participants']):
            line = _escape(participant['display_name'])
            if participant['position']:
                line += f" – {_escape(participant['position'])}"
            story.append(Paragraph(line + ('.' if index == last else ';'), styles['person']))
    else:
        story.append(Paragraph('—', styles['body']))

    # -- Повестка
    story.append(Paragraph('Повестка:', styles['heading']))
    if document['agenda']:
        for index, item in enumerate(document['agenda'], start=1):
            story.append(Paragraph(_escape(item), styles['item'], bulletText=f'{index}.'))
    else:
        story.append(Paragraph('—', styles['body']))

    # -- Слушали
    story.append(Paragraph('Слушали:', styles['heading']))
    if document['speeches']:
        for speech in document['speeches']:
            story.append(Paragraph(f"{_escape(speech['speaker'])}:", styles['speaker']))
            story.append(Paragraph(_escape(speech['text']), styles['speech']))
    else:
        story.append(Paragraph('—', styles['body']))

    # -- Решили
    story.append(Paragraph('Решили:', styles['heading']))
    if document['decisions']:
        for index, decision in enumerate(document['decisions'], start=1):
            story.append(
                Paragraph(_escape(decision['text']), styles['item'], bulletText=f'{index}.')
            )
            if decision['assignees']:
                story.append(
                    Paragraph(
                        f"Ответственный: {_escape(', '.join(decision['assignees']))}",
                        styles['fact'],
                    )
                )
            if decision['due_date']:
                story.append(
                    Paragraph(f"Срок: {_short_date(decision['due_date'])}", styles['fact'])
                )
    else:
        story.append(Paragraph('Решения не приняты.', styles['body']))

    # -- Протокол согласован
    #
    # Signature lines, as on the paper original: who has to sign and in which
    # role. The electronic decision, its date and the round it belongs to stay
    # on the protocol page — a printed form is something people sign.
    if document['approvals']:
        story.append(Paragraph('Протокол согласован:', styles['heading']))
        rows = []
        for approval in document['approvals']:
            line = _escape(approval['display_name'])
            if approval['position']:
                line += f" – {_escape(approval['position'])}"
            rows.append([Paragraph(line, styles['body']), ''])
        signatures = Table(rows, colWidths=[width - 45 * mm, 45 * mm], hAlign='LEFT')
        signatures.setStyle(
            TableStyle(
                [
                    ('LINEBELOW', (1, 0), (1, -1), 0.7, colors.black),
                    ('VALIGN', (0, 0), (-1, -1), 'BOTTOM'),
                    ('LEFTPADDING', (0, 0), (-1, -1), 0),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 7),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(signatures)

    # -- Подготовил
    story.append(Spacer(1, 18))
    story.append(Paragraph('Подготовил:', styles['prepared']))
    story.append(Paragraph(_escape(document['author_name']), styles['prepared']))
    story.append(Paragraph(_short_date(document['prepared_at']), styles['prepared']))

    page.build(story)
    return buffer.getvalue()


def protocol_pdf_filename(document):
    """A safe file name: type code, number."""
    code = document['protocol'].protocol_type.code.lower()
    return f"protocol-{code}-{document['number']}.pdf"
