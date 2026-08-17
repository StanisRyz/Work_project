"""Standalone `.xlsx` generation for the «Проработка» journal.

Adapted from `build_xlsx()` in the source repository's server prototype: the
same five package parts written directly through `zipfile`, so the export
needs no spreadsheet dependency at all.

The journal is exported whole. Production data is filled in over days, so a
row is written whether or not it has been confirmed, and a value that does
not exist yet becomes an empty cell — never a `None`, a `null`, a stand-in
zero or an empty `<v>` element, any of which would quietly turn into a number
in the recipient's spreadsheet.
"""
import io
import math
import zipfile
from html import escape

EXPORT_HEADERS = (
    'd, мм', 'D, мм', 'b, мм', 'h, мм', 'δ, мм', 'Калибровка, мм', 'КС',
    'Р.В., ч', 'Ед.В.П.', 'Ф.В.П., ч', 'Ф.В.Ед, ч', '1С, ч', 'Сотрудник',
)

NO_CALIBRATION = 'Нет'

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
_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
    '<sheets><sheet name="Проработка" sheetId="1" r:id="rId1"/></sheets></workbook>'
)
_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '</Relationships>'
)


def _column_name(index):
    result = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _text(value):
    """A cell Excel must read as text, even when it looks like a number."""
    return ('text', str(value))


def _number(value):
    """A numeric cell, or an empty one when the value is missing or unusable."""
    if value is None or value == '':
        return ('empty', None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ('empty', None)
    if not math.isfinite(number):
        return ('empty', None)
    return ('number', number)


def _calibration_cell(entry):
    """`Нет` for a non-calibrated core, the radius for a calibrated one."""
    if not entry.calibration_enabled or not entry.calibration_diameter_mm:
        return _text(NO_CALIBRATION)
    return _number(entry.calibration_diameter_mm)


def _optional_text(value):
    return _text(value) if value else ('empty', None)


def _entry_row(entry):
    return (
        _number(entry.d), _number(entry.outer_diameter), _number(entry.b),
        _number(entry.height_mm), _number(entry.tape_thickness_mm),
        _calibration_cell(entry), _number(entry.complexity_coefficient),
        _number(entry.total_time_seconds / 3600 if entry.total_time_seconds is not None else None),
        _number(entry.batch_quantity), _number(entry.actual_batch_time_hours),
        _number(entry.actual_unit_time_hours), _number(entry.one_c_hours),
        _optional_text(entry.employee_name),
    )


def _cell_xml(reference, cell):
    kind, value = cell
    if kind == 'empty':
        # No `<v>` at all: the cell exists in the sheet and holds nothing.
        return f'<c r="{reference}"/>'
    if kind == 'text':
        return f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    return f'<c r="{reference}"><v>{value!r}</v></c>'


def _worksheet_xml(entries):
    rows = [tuple(_text(header) for header in EXPORT_HEADERS)]
    rows.extend(_entry_row(entry) for entry in entries)
    xml_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = ''.join(
            _cell_xml(f'{_column_name(column_index)}{row_index}', cell)
            for column_index, cell in enumerate(row, 1)
        )
        xml_rows.append(f'<row r="{row_index}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )


def build_journal_xlsx(entries):
    """Return the `.xlsx` bytes for the given journal entries.

    An empty journal is a valid workbook too — it just carries the header row.
    """
    files = {
        '[Content_Types].xml': _CONTENT_TYPES,
        '_rels/.rels': _ROOT_RELS,
        'xl/workbook.xml': _WORKBOOK,
        'xl/_rels/workbook.xml.rels': _WORKBOOK_RELS,
        'xl/worksheets/sheet1.xml': _worksheet_xml(entries),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as workbook:
        for path, content in files.items():
            workbook.writestr(path, content.encode('utf-8'))
    return output.getvalue()
