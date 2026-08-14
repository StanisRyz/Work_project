"""Standalone `.xlsx` generation for the «Проработка» journal.

Adapted from `build_xlsx()` in the source repository's server prototype: the
same five package parts, the same column order and the same numeric cells.
It writes the OOXML parts directly through `zipfile`, so the export needs no
spreadsheet dependency at all.
"""
import io
import zipfile
from html import escape

EXPORT_HEADERS = (
    'd, мм', 'D, мм', 'b, мм', 'Высота, мм',
    'Скорость навивки, сек/мм', 'КС', 'Расчётное время, ч',
    'Единиц в партии', 'Фактическое время партии, ч',
    'Фактическое время единицы, ч',
)

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


def _entry_row(entry):
    return (
        entry.d, entry.outer_diameter, entry.b, entry.height_mm,
        entry.standard_coefficient, entry.complexity_coefficient,
        entry.total_time_seconds / 3600, entry.batch_quantity,
        entry.actual_batch_time_hours, entry.actual_unit_time_hours,
    )


def _worksheet_xml(entries):
    rows = [EXPORT_HEADERS]
    rows.extend(_entry_row(entry) for entry in entries)
    xml_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(row, 1):
            reference = f'{_column_name(column_index)}{row_index}'
            if row_index == 1:
                cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            else:
                # A bare `<v>` cell: the workbook keeps every value numeric.
                cells.append(f'<c r="{reference}"><v>{value}</v></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>'
    )


def build_journal_xlsx(entries):
    """Return the `.xlsx` bytes for the given confirmed journal entries."""
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
