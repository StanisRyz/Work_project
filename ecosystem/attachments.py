"""The one file-attachment policy every module uploads through.

Acts and protocols keep separate tables, separate upload paths and separate
permission rules — they are independent domains — but a file that is too large
or of the wrong type must be refused identically wherever it is offered.
Duplicating the extension set and the size limit is how two policies quietly
drift apart, so both live here and both apps import them.

Nothing about *who* may attach anything is decided here: that stays in each
app's own `permissions.py`.
"""

from django.core.exceptions import ValidationError


ALLOWED_ATTACHMENT_EXTENSIONS = {
    '.pdf',
    '.doc',
    '.docx',
    '.xls',
    '.xlsx',
    '.png',
    '.jpg',
    '.jpeg',
    '.webp',
    '.txt',
}
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024


def attachment_extension(filename):
    """The lower-cased suffix of an uploaded name, or '' when there is none.

    Deliberately not `Path(filename).suffix`: the name comes from the browser
    and may carry a path separator of the client's platform, which `Path` would
    interpret on the server's. Splitting on the last dot reads the name as the
    opaque string it is.
    """
    parts = (filename or '').rsplit('.', 1)
    return f'.{parts[1].lower()}' if len(parts) == 2 else ''


def validate_attachment_upload(uploaded_file):
    """Refuse a file no attachment table may store. Returns it unchanged.

    Raises `ValidationError`, so a `clean_file()` in any app reports it as an
    ordinary field error rather than a 500.
    """
    if attachment_extension(uploaded_file.name) not in ALLOWED_ATTACHMENT_EXTENSIONS:
        raise ValidationError('Недопустимый тип файла.')
    if uploaded_file.size > MAX_ATTACHMENT_SIZE:
        raise ValidationError('Размер файла превышает допустимый лимит.')
    return uploaded_file


def format_file_size(size_bytes):
    """Bytes as the short Russian label the attachment cards show."""
    if size_bytes < 1024:
        return f'{size_bytes} Б'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} КБ'
    return f'{size_bytes / (1024 * 1024):.1f} МБ'
