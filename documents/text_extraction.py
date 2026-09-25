"""The readable text of an uploaded version, for «поиск по тексту».

Run once, when a version is stored (`documents/services.py`), and kept on
`DocumentVersion.text_content`; `manage.py reindex_documents` fills it for
versions uploaded before the field existed. Never raises into the upload: a
file nothing can be read from — a scan, an image, a damaged PDF — simply gets
no text and is found by its name only.

* PDF — `pypdf`, pure Python, page by page;
* Word / Excel / PowerPoint (`.docx`, `.xlsx`, `.pptx`) — the XML inside the
  zip, no library at all;
* plain text — decoded as UTF-8, then CP1251 (what Windows editors on the
  plant's machines still write).

Capped at `MAX_TEXT_LENGTH` characters: the column is for finding a document,
not for storing it twice.
"""

import logging
import re
import zipfile
from html import unescape
from io import BytesIO

from ecosystem.logging_utils import log_event


logger = logging.getLogger('ecosystem.documents')

MAX_TEXT_LENGTH = 400_000
MAX_PDF_PAGES = 400

_XML_TAG = re.compile(r'<[^>]+>')
_WHITESPACE = re.compile(r'[ \t\r\f\v]+')
_BLANK_LINES = re.compile(r'\n{3,}')

# Which parts of an Office package hold the words, per extension.
_OFFICE_PARTS = {
    'docx': re.compile(r'^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$'),
    'xlsx': re.compile(r'^xl/sharedStrings\.xml$'),
    'pptx': re.compile(r'^ppt/slides/slide\d+\.xml$'),
}
# Paragraph-ish boundaries inside that XML, turned into line breaks so words
# from two paragraphs never run together.
_OFFICE_BREAKS = re.compile(r'</(w:p|a:p|si|w:tr)>|<w:br[^>]*/>|<w:tab[^>]*/>')


def _tidy(text):
    text = _WHITESPACE.sub(' ', text or '')
    text = _BLANK_LINES.sub('\n\n', text)
    return text.strip()[:MAX_TEXT_LENGTH]


def _pdf_text(data):
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - the requirement is pinned
        return ''
    reader = PdfReader(BytesIO(data))
    parts = []
    for index, page in enumerate(reader.pages):
        if index >= MAX_PDF_PAGES:
            break
        parts.append(page.extract_text() or '')
        if sum(len(part) for part in parts) > MAX_TEXT_LENGTH:
            break
    return '\n'.join(parts)


def _office_text(data, extension):
    pattern = _OFFICE_PARTS[extension]
    parts = []
    with zipfile.ZipFile(BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if not pattern.match(name):
                continue
            xml = archive.read(name).decode('utf-8', errors='ignore')
            xml = _OFFICE_BREAKS.sub('\n', xml)
            parts.append(unescape(_XML_TAG.sub(' ', xml)))
    return '\n'.join(parts)


def _plain_text(data):
    for encoding in ('utf-8', 'cp1251'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='ignore')


def extract_text(data, extension):
    """The text of one file's bytes, or '' when there is none to be had."""
    extension = (extension or '').lower()
    try:
        if extension == 'pdf':
            text = _pdf_text(data)
        elif extension in _OFFICE_PARTS:
            text = _office_text(data, extension)
        elif extension in {'txt', 'md', 'csv'}:
            text = _plain_text(data)
        else:
            return ''
    except Exception as exc:  # noqa: BLE001 - any unreadable file is «no text»
        log_event(
            logger, 'WARNING', 'documents.text_extraction_failed',
            extension=extension, error_type=type(exc).__name__, outcome='skipped',
        )
        return ''
    return _tidy(text)


def extract_version_text(version):
    """Read a stored version's file and return its text ('' on any failure)."""
    if not version.file:
        return ''
    try:
        with version.file.open('rb') as handle:
            data = handle.read()
    except OSError:
        return ''
    return extract_text(data, version.extension)
