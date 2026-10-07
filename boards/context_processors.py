"""Whether the «Доски» and «Заявки» navigation entries are drawn, for every page.

The sidebar is part of every screen, so the answer cannot come from
`boards/views.py`; a context processor asks the very `can_use_boards()` the
board views enforce, so the link and the pages behind it never disagree.
Hiding the link is a convenience, not the protection: every board view asks
the right again and answers 403 to a URL typed by hand.
"""

from django.utils.functional import SimpleLazyObject

from .permissions import can_use_boards, can_use_requests


def boards_access(request):
    return {'can_use_boards': can_use_boards(getattr(request, 'user', None))}


def requests_access(request):
    """«Заявки» in the menu: an active employee, while some live board takes
    requests (`can_use_requests()`). Lazy — the `EXISTS` runs only when a
    template asks, so a live fragment rendered with a `RequestContext` never
    pays for it."""
    user = getattr(request, 'user', None)
    return {'can_use_requests': SimpleLazyObject(lambda: can_use_requests(user))}
