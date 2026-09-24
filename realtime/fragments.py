"""The fingerprint of a live block that holds a form.

A block such as the act's «Проработка» tab or the protocol editor is never
replaced while the user has unsaved input; the client raises the conflict
banner instead. Before this fingerprint existed the client had no way to tell
*whether the block had changed at all*: every SSE reconnect (the stream ends by
itself after `REALTIME_MAX_CONNECTION_SECONDS`), every recovery sync and every
movement of a global revision token refetched the block and, for a dirty form,
announced a conflict that did not exist — and the only way out the banner
offered was a reload that threw the typed text away.

The fingerprint is a hash of exactly the markup the fragment endpoint would
send, so «unchanged» means «a reload would render the same block»: the page
renders it next to the initial markup, the fragment returns it next to the new
markup, and the client compares the two. Nothing here is a permission or a
source of HTML; it only answers «did the server state of this block move?».

The CSRF input is removed first: Django masks the token with a fresh salt on
every render, so two renders of the same state would otherwise never match.
"""

import hashlib
import re


_CSRF_INPUT = re.compile(r'<input\b[^>]*\bname="csrfmiddlewaretoken"[^>]*>')


def content_revision(html):
    """A short, stable fingerprint of one rendered live block."""
    normalized = _CSRF_INPUT.sub('', str(html))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:32]
