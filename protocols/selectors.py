"""Read-side queries for the Protocols pages.

Reads only. Every mutation stays in `protocols/services.py`, and every
visibility rule stays in `protocols/permissions.py` — this module just shapes
what the templates render.
"""

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department

from .models import Protocol, ProtocolApproval, ProtocolType


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


# --------------------------------------------------------------------------
# Approval read side
#
# The minimum the approval UI stage needs, and nothing more: the current
# round, its progress, one user's place in it, and the earlier rounds kept for
# audit. Reads only — every decision stays in `protocols/services.py`.
# --------------------------------------------------------------------------


def get_current_revision_approvals(protocol):
    """The approval rows of the revision the protocol is on right now.

    A protocol that has never been sent is on revision 0 and has none, which
    is an empty queryset rather than a special case.
    """
    return (
        ProtocolApproval.objects.filter(protocol=protocol, revision=protocol.revision)
        .select_related('user', 'task', 'task__status')
        .order_by('pk')
    )


def get_approval_progress(protocol):
    """How far the current round has got: `{revision, total, approved, pending}`.

    Counted from the approval rows, never from the protocol status: `APPROVAL`
    with nothing pending is exactly the state finalization is about to leave,
    and the counts must keep saying so.
    """
    approvals = get_current_revision_approvals(protocol)
    total = 0
    approved = 0
    pending = 0
    for approval in approvals:
        total += 1
        if approval.status == ProtocolApproval.Status.APPROVED:
            approved += 1
        elif approval.status == ProtocolApproval.Status.PENDING:
            pending += 1
    return {
        'revision': protocol.revision,
        'total': total,
        'approved': approved,
        'pending': pending,
    }


def get_user_approval(protocol, user):
    """This user's approval row for the current revision, or `None`.

    `None` covers all of «not an approver», «this revision does not require
    them» and «not signed in» — the caller renders nothing in every case.
    """
    if not getattr(user, 'is_authenticated', False):
        return None
    return (
        ProtocolApproval.objects.filter(protocol=protocol, revision=protocol.revision, user=user)
        .select_related('task', 'task__status')
        .first()
    )


def get_approvals_by_revision(protocol):
    """Every round the protocol has been through, newest revision first.

    Returns `[{'revision': n, 'approvals': [...]}, ...]`. Earlier revisions are
    kept forever and are the audit answer to «who signed what, and when» — they
    never count towards the current round.
    """
    groups = []
    rows = (
        ProtocolApproval.objects.filter(protocol=protocol)
        .select_related('user', 'task')
        .order_by('-revision', 'pk')
    )
    for approval in rows:
        if not groups or groups[-1]['revision'] != approval.revision:
            groups.append({'revision': approval.revision, 'approvals': []})
        groups[-1]['approvals'].append(approval)
    return groups
