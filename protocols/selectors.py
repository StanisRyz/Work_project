"""Read-side queries for the Protocols pages.

Reads only. Every mutation stays in `protocols/services.py`, and every
visibility rule stays in `protocols/permissions.py` — this module just shapes
what the templates render.
"""

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department

from .models import Protocol, ProtocolType


# The registry tabs. «В работе» is written against all three live statuses even
# though only drafts exist today, so the approval stage adds no tab logic.
TAB_STATUSES = {
    'work': (Protocol.Status.DRAFT, Protocol.Status.APPROVAL, Protocol.Status.REVISION),
    'archive': (Protocol.Status.ARCHIVED,),
}
DEFAULT_TAB = 'work'


def get_readable_protocols_queryset():
    """Every authenticated user may read every protocol."""
    return Protocol.objects.select_related('protocol_type', 'author')


def build_protocol_list_state(params):
    tab = params.get('tab') if params else None
    if tab not in TAB_STATUSES:
        tab = DEFAULT_TAB
    protocols = get_readable_protocols_queryset().filter(status__in=TAB_STATUSES[tab])
    return {
        'tab': tab,
        'protocols': protocols,
        'status_labels': dict(Protocol.Status.choices),
    }


def get_active_protocol_types():
    return ProtocolType.objects.filter(is_active=True).order_by('display_order', 'name')


def get_protocol_history_groups(protocol):
    """History events grouped into local-date buckets, newest first."""
    groups = []
    for event in protocol.history_events.select_related('actor'):
        event_date = timezone.localtime(event.created_at).date()
        if not groups or groups[-1]['date'] != event_date:
            groups.append({'date': event_date, 'events': []})
        groups[-1]['events'].append(event)
    return groups


def get_editor_directory():
    """The department/employee options the editor's selectors are built from.

    The same mechanism the ТО analysis form already uses: the page renders every
    active employee once, tagged with `data-department-id`, and the browser only
    filters what is already there. No directory endpoint is involved, and the
    server re-checks the department of every submitted employee anyway.
    """
    return {
        'departments': Department.objects.filter(is_active=True),
        'employees': User.objects.filter(is_active=True, userprofile__is_active=True)
        .select_related('userprofile__department')
        .order_by('last_name', 'first_name', 'username'),
    }
