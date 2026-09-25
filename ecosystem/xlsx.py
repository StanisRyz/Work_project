"""A minimal `.xlsx` writer for registry exports — no spreadsheet dependency.

The same five package parts `calculator/export.py` writes, generalised to any
header row and rows of plain values. A registry export is what the registry
shows, so each module builds its rows from its own state builder and only the
file format lives here.

Values: `str` → a text cell, `int`/`float` → a number, `date`/`datetime` →
text `ДД.ММ.ГГГГ` (a date the recipient reads, never a serial number that
changes meaning with the locale), `None`/`''` → an empty cell with no `<v>`.
"""

import io
import math
import re
import zipfile
from datetime import date, datetime
from html import escape

from django.http import HttpResponse
from django.utils import timezone

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>'
)
_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '</Relationships>'
)
# Excel refuses a sheet name longer than 31 characters or holding any of these.
_SHEET_NAME_FORBIDDEN = re.compile(r'[\[\]:*?/\\]')
# XML 1.0 cannot carry these control characters at all, even escaped.
_XML_INVALID = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _column_name(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _cell_xml(reference, value):
    if value is None or value == '':
        return f'<c r="{reference}"/>'
    if isinstance(value, bool):
        value = 'Да' if value else 'Нет'
    if isinstance(value, datetime):
        value = timezone.localtime(value) if timezone.is_aware(value) else value
        value = value.strftime('%d.%m.%Y %H:%M')
    elif isinstance(value, date):
        value = value.strftime('%d.%m.%Y')
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return f'<c r="{reference}"/>'
        return f'<c r="{reference}"><v>{value!r}</v></c>'
    text = _XML_INVALID.sub('', str(value))
    return f'<c r="{reference}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>'


def build_xlsx(sheet_name, headers, rows):
    """The `.xlsx` bytes: `headers` as row 1, then one row per item of `rows`."""
    sheet = _SHEET_NAME_FORBIDDEN.sub(' ', sheet_name)[:31] or 'Лист1'
    xml_rows = []
    for row_index, row in enumerate([list(headers), *rows], 1):
        cells = ''.join(
            _cell_xml(f'{_column_name(column)}{row_index}', value)
            for column, value in enumerate(row, 1)
        )
        xml_rows.append(f'<row r="{row_index}">{cells}</row>')
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', _CONTENT_TYPES)
        archive.writestr('_rels/.rels', _ROOT_RELS)
        archive.writestr('xl/workbook.xml', workbook.encode('utf-8'))
        archive.writestr('xl/_rels/workbook.xml.rels', _WORKBOOK_RELS)
        archive.writestr('xl/worksheets/sheet1.xml', worksheet.encode('utf-8'))
    return output.getvalue()


def xlsx_response(filename_stem, sheet_name, headers, rows):
    """A download of `build_xlsx(...)` named `<stem>_<ДД.ММ.ГГГГ>.xlsx`.

    `filename_stem` is ASCII on purpose (`acts`, `tasks`, …): a Cyrillic
    `Content-Disposition` needs RFC 5987 encoding that older browsers on the
    plant's machines mishandle, and the sheet inside carries the Russian name.
    """
    response = HttpResponse(
        build_xlsx(sheet_name, headers, rows),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    stamp = timezone.localdate().strftime('%Y-%m-%d')
    response['Content-Disposition'] = f'attachment; filename="{filename_stem}_{stamp}.xlsx"'
    response['Cache-Control'] = 'no-store, private'
    return response
