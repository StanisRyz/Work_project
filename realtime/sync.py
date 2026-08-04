"""Recovery state for one user: opaque revision tokens, nothing else.

SSE is best-effort. A client that missed events — a dropped connection, a Redis
restart, a laptop lid closed for an hour — asks this service «has anything I can
see changed?» and gets back short opaque tokens plus a couple of safe counters.
Comparing tokens tells the client *which block* to refetch; the blocks
themselves are still fetched through the ordinary permission-checked fragment
endpoints.

Two rules shape everything here:

* a token is derived only from rows the user may already see, through the very
  same querysets the pages use, so it can never reveal that somebody else's
  object exists;
* a token is a hash of aggregates, never a serialized row, so no business text,
  no identifier of a foreign object and no personal data can leak through it.
"""

import hashlib
from datetime import datetime, timezone as dt_timezone

from django.db.models import Count, Max
from django.utils import timezone


SCHEMA_VERSION = 1

# The blocks a client can refresh independently.
REVISION_NOTIFICATIONS = 'notifications'
REVISION_TASKS = 'tasks'
REVISION_ACTS = 'acts'
REVISION_COMMENTS = 'comments'
REVISION_ACTIVITIES = 'activities'

REVISION_KEYS = (
    REVISION_NOTIFICATIONS,
    REVISION_TASKS,
    REVISION_ACTS,
    REVISION_COMMENTS,
    REVISION_ACTIVITIES,
)

TOKEN_LENGTH = 16


def _stamp(value):
    """Render a timestamp deterministically, or `-` when there is none."""
    if isinstance(value, datetime):
        return value.astimezone(dt_timezone.utc).isoformat()
    return '-'


def _token(*parts):
    """Short, stable, opaque token built from aggregate values only."""
    payload = '|'.join(
        _stamp(part) if isinstance(part, datetime) else str(part if part is not None else '-')
        for part in parts
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:TOKEN_LENGTH]


def _notifications_revision(user):
    from notifications.models import Notification

    scoped = Notification.objects.filter(recipient=user)
    aggregate = scoped.aggregate(
        total=Count('pk'),
        last_created=Max('created_at'),
        last_read=Max('read_at'),
    )
    unread = scoped.filter(is_read=False).count()
    return (
        _token(
            'n',
            unread,
            aggregate['total'],
            aggregate['last_created'],
            aggregate['last_read'],
        ),
        unread,
    )


def _tasks_revision(user):
    from tasks.permissions import get_visible_tasks_queryset

    visible = get_visible_tasks_queryset(user)
    aggregate = visible.aggregate(
        total=Count('pk', distinct=True),
        last_created=Max('created_at'),
        last_updated=Max('updated_at'),
        last_completed=Max('completed_at'),
        assignments=Count('assignees', distinct=True),
    )
    # Status mix, so completing a task changes the token even when the row
    # count and the timestamps happen to look the same.
    statuses = tuple(
        sorted(
            visible.values_list('status__code')
            .annotate(count=Count('pk', distinct=True))
            .values_list('status__code', 'count')
        )
    )
    return _token(
        't',
        aggregate['total'],
        aggregate['last_created'],
        aggregate['last_updated'],
        aggregate['last_completed'],
        aggregate['assignments'],
        statuses,
    )


def _visible_acts(user):
    """Active and archived acts the user may see, as two querysets."""
    from acts.permissions import get_archived_acts_queryset
    from acts.services import get_visible_acts_for_user

    active = get_visible_acts_for_user(user).exclude(status__code='ARCHIVED')
    return active, get_archived_acts_queryset(user)


def _acts_revision(user):
    active, archived = _visible_acts(user)
    active_aggregate = active.aggregate(total=Count('pk', distinct=True), last=Max('updated_at'))
    archived_aggregate = archived.aggregate(
        total=Count('pk', distinct=True), last=Max('updated_at')
    )
    statuses = tuple(
        sorted(
            active.values_list('status__code')
            .annotate(count=Count('pk', distinct=True))
            .values_list('status__code', 'count')
        )
    )
    return _token(
        'a',
        active_aggregate['total'],
        active_aggregate['last'],
        archived_aggregate['total'],
        archived_aggregate['last'],
        statuses,
    )


def _comments_revision(user):
    """Comments and history inside the acts this user may see."""
    from acts.models import ActComment, ActHistoryEvent

    active, archived = _visible_acts(user)
    act_ids = list(active.values_list('pk', flat=True)) + list(
        archived.values_list('pk', flat=True)
    )
    comments = ActComment.objects.filter(act_id__in=act_ids).aggregate(
        total=Count('pk'), last=Max('created_at')
    )
    history = ActHistoryEvent.objects.filter(act_id__in=act_ids).aggregate(
        total=Count('pk'), last=Max('created_at')
    )
    return _token(
        'c',
        comments['total'],
        comments['last'],
        history['total'],
        history['last'],
    )


def _activities_revision(user):
    """Tasks linked to acts, as the act detail «связанные мероприятия» shows them."""
    from tasks.permissions import get_visible_tasks_queryset

    linked = get_visible_tasks_queryset(user).filter(act__isnull=False)
    aggregate = linked.aggregate(
        total=Count('pk', distinct=True),
        last_updated=Max('updated_at'),
        last_completed=Max('completed_at'),
    )
    statuses = tuple(
        sorted(
            linked.values_list('status__code')
            .annotate(count=Count('pk', distinct=True))
            .values_list('status__code', 'count')
        )
    )
    return _token(
        'v',
        aggregate['total'],
        aggregate['last_updated'],
        aggregate['last_completed'],
        statuses,
    )


def build_sync_state(user):
    """Return the user's current revision snapshot.

    A fixed, small number of aggregate queries — it never loads rows, so the
    cost does not grow with how much data the user can see.
    """
    notifications_token, unread = _notifications_revision(user)
    return {
        'schema_version': SCHEMA_VERSION,
        'generated_at': timezone.now().isoformat(),
        'revisions': {
            REVISION_NOTIFICATIONS: notifications_token,
            REVISION_TASKS: _tasks_revision(user),
            REVISION_ACTS: _acts_revision(user),
            REVISION_COMMENTS: _comments_revision(user),
            REVISION_ACTIVITIES: _activities_revision(user),
        },
        'unread_notifications': unread,
    }
