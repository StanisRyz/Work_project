"""Whether the «Доски» navigation entry is drawn, for every page.

The sidebar is part of every screen, so the answer cannot come from
`boards/views.py`; a context processor asks the very `can_use_boards()` the
board views enforce, so the link and the pages behind it never disagree.
Hiding the link is a convenience, not the protection: every board view asks
the right again and answers 403 to a URL typed by hand.
"""

from .permissions import can_use_boards


def boards_access(request):
    return {'can_use_boards': can_use_boards(getattr(request, 'user', None))}
