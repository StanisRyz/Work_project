"""What a document version can be shown as, without leaving the page.

Preview is a *display* decision and a safety one, and both live here rather
than in a view or a template. Two rules:

* the content type served inline is derived from the file's own extension
  against the map below — never from the `content_type` the browser sent at
  upload, which is user input and can claim anything;
* anything not in the map has no preview at all. The page says so and offers
  the download instead.

No external viewer, no converter and no JavaScript library: a PDF goes into an
`<iframe>` and an image into an `<img>`, both served by
`documents:document_version_preview` from this project's own origin.

Office formats (`.docx`, `.xlsx`, …) deliberately have no preview. Rendering
them would mean either shipping a converter or sending corporate documents to
a third-party service. If a stage later generates PDF renditions, it adds a
`preview_file` to `DocumentVersion` and one branch in `describe_preview()`;
nothing else on the page changes.
"""


KIND_PDF = 'pdf'
KIND_IMAGE = 'image'
KIND_TEXT = 'text'
KIND_NONE = ''

# The only types ever sent with `Content-Disposition: inline`. Every entry is
# something a browser renders in a sandboxed context: no HTML and no SVG,
# which would execute as same-origin script.
INLINE_TYPES = {
    'pdf': ('application/pdf', KIND_PDF),
    'png': ('image/png', KIND_IMAGE),
    'jpg': ('image/jpeg', KIND_IMAGE),
    'jpeg': ('image/jpeg', KIND_IMAGE),
    'webp': ('image/webp', KIND_IMAGE),
    'gif': ('image/gif', KIND_IMAGE),
    'bmp': ('image/bmp', KIND_IMAGE),
    'txt': ('text/plain; charset=utf-8', KIND_TEXT),
    'md': ('text/plain; charset=utf-8', KIND_TEXT),
}

UNAVAILABLE_MESSAGE = (
    'Предпросмотр для этого типа файла недоступен — скачайте документ, '
    'чтобы открыть его в привычном приложении.'
)


def inline_content_type(extension):
    """The type this extension may be served inline as, or None."""
    entry = INLINE_TYPES.get((extension or '').lower())
    return entry[0] if entry else None


def describe_preview(version):
    """How to show one version in the page: `{kind, message}`.

    `kind` is `''` when there is nothing to show, and the caller renders
    `message` plus a download link instead of a viewer.
    """
    if version is None:
        return {'kind': KIND_NONE, 'message': 'У документа нет ни одной версии.'}
    entry = INLINE_TYPES.get(version.extension)
    if entry is None:
        return {'kind': KIND_NONE, 'message': UNAVAILABLE_MESSAGE}
    return {'kind': entry[1], 'message': ''}
