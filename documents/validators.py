"""What the documentation library accepts on upload.

Deliberately its own policy and not `ecosystem/attachments.py`: an act
attachment is evidence pinned to one record, while a corporate document is a
library file — bigger, and of a wider set of office formats. The two must not
drift into each other, so this module states the document rules in full and
imports only the shared size formatter.

Three independent guards, in order:

1. an explicit denylist of executable and script suffixes, so a dangerous file
   is refused by name even if the allowlist below is ever widened carelessly;
2. an allowlist, which is what actually decides;
3. a size limit.

Nothing here inspects file *content*: this is upload hygiene, not scanning.
"""

from django.core.exceptions import ValidationError


# 25 MB. Larger than an act attachment (10 MB) because a scanned regulatory
# document or a training deck legitimately is, and small enough that a single
# upload cannot fill the media volume by accident.
MAX_DOCUMENT_SIZE = 25 * 1024 * 1024

# Refused first and unconditionally. Not a security boundary on its own —
# files are stored under generated names and are never served from a public
# media URL — but a library that visibly declines to hold an .exe is one an
# administrator can reason about.
BLOCKED_DOCUMENT_EXTENSIONS = frozenset({
    '.exe', '.com', '.bat', '.cmd', '.msi', '.msp', '.scr', '.pif', '.cpl',
    '.dll', '.sys', '.drv', '.jar', '.js', '.jse', '.vbs', '.vbe', '.wsf',
    '.wsh', '.ps1', '.psm1', '.sh', '.bash', '.py', '.pyc', '.pl', '.php',
    '.hta', '.reg', '.lnk', '.app', '.deb', '.rpm', '.apk',
})

# What the library actually stores: office documents, drawings-as-PDF, images
# for instructions, and plain text. Archives are deliberately absent — an
# archive hides what it carries from every check above.
ALLOWED_DOCUMENT_EXTENSIONS = frozenset({
    '.pdf',
    '.doc', '.docx', '.rtf', '.odt',
    '.xls', '.xlsx', '.csv', '.ods',
    '.ppt', '.pptx', '.odp',
    '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tif', '.tiff',
    '.txt', '.md',
})

MAX_DOCUMENT_NAME_LENGTH = 255


def document_extension(filename):
    """The lower-cased suffix of an uploaded name, or '' when there is none.

    Splitting on the last dot rather than using `Path().suffix`: the name
    arrives from the browser and may carry the *client's* path separators,
    which `Path` would interpret with the server's rules.
    """
    parts = (filename or '').rsplit('.', 1)
    return f'.{parts[1].lower()}' if len(parts) == 2 else ''


def safe_document_name(filename):
    """A display name with every path component and control character removed.

    The result is never used to build a storage path — `document_upload_to()`
    generates that — but it is shown in the browser and sent back as the
    download filename, so it must not carry `..`, a separator, or a newline
    that could be smuggled into a response header.
    """
    raw = (filename or '').strip()
    # Both separators, whatever the client's platform was.
    for separator in ('\\', '/'):
        raw = raw.rsplit(separator, 1)[-1]
    cleaned = ''.join(character for character in raw if character.isprintable()).strip()
    # A name that was only dots, or empty, describes no file.
    if not cleaned or set(cleaned) <= {'.'}:
        return 'file'
    return cleaned[:MAX_DOCUMENT_NAME_LENGTH]


def validate_document_upload(uploaded_file):
    """Refuse a file the library must not store. Returns it unchanged.

    Raises `ValidationError`, so a form's `clean_file()` reports it as an
    ordinary field error rather than a 500.
    """
    extension = document_extension(uploaded_file.name)
    if extension in BLOCKED_DOCUMENT_EXTENSIONS:
        raise ValidationError('Исполняемые файлы загружать нельзя.')
    if extension not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError('Недопустимый тип файла.')
    if uploaded_file.size > MAX_DOCUMENT_SIZE:
        raise ValidationError('Размер файла превышает 25 МБ.')
    return uploaded_file
