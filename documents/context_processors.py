"""Whether the «Документация» navigation entry is drawn, for every page.

The sidebar is included by `base.html` on every screen in the application, so
the answer cannot come from `documents/views.py` — it has to be available in a
template rendered by `acts`, `tasks` or a calculator. A context processor is
the smallest thing that does that, and it asks the same
`can_view_documents()` the documentation views themselves enforce, so the link
and the pages behind it can never disagree.

Hiding the link is a convenience, not the protection: every view in
`documents/views.py` re-checks the permission and answers 403 to a URL typed by
hand.
"""

from .permissions import can_view_documents


def documentation_access(request):
    return {'can_view_documentation': can_view_documents(getattr(request, 'user', None))}
