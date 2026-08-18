"""Recovery state for one user: opaque revision tokens, nothing else.

SSE is best-effort. A client that missed events — a dropped connection, a Redis
restart, a laptop lid closed for an hour — asks this service «has anything I can
see changed?» and gets back short opaque tokens plus a couple of safe counters.
Comparing tokens tells the client *which block* to refetch; the blocks
themselves are still fetched through the ordinary permission-checked fragment
endpoints.

Three rules shape everything here:

* a token is derived only from rows the user may already see, through the very
  same permission rules the pages use, so it can never reveal that somebody
  else's object exists;
* a token is a hash of aggregates, never a serialized row, so no business text,
  no identifier of a foreign object and no personal data can leak through it;
* the cost is a fixed, small number of aggregate queries. Nothing here loads
  rows or materialises identifiers in Python, so a user who can see thousands
  of acts costs exactly what a user who can see none costs. The budget is
  pinned by `realtime/tests/test_sync.py`.
"""

import hashlib
from datetime import datetime, timezone as dt_timezone

from django.db.models import Count, Max, Q
from django.utils import timezone


SCHEMA_VERSION = 1

# The blocks a client can refresh independently.
REVISION_NOTIFICATIONS = 'notifications'
REVISION_TASKS = 'tasks'
REVISION_ACTS = 'acts'
REVISION_COMMENTS = 'comments'
REVISION_ACTIVITIES = 'activities'
REVISION_WORKUP = 'workup'

REVISION_KEYS = (
    REVISION_NOTIFICATIONS,
    REVISION_TASKS,
    REVISION_ACTS,
    REVISION_COMMENTS,
    REVISION_ACTIVITIES,
    REVISION_WORKUP,
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

    # One query: the unread count is a filtered aggregate rather than a second
    # `.count()` round trip over the same rows.
    aggregate = Notification.objects.filter(recipient=user).aggregate(
        total=Count('pk'),
        unread=Count('pk', filter=Q(is_read=False)),
        last_created=Max('created_at'),
        last_read=Max('read_at'),
    )
    unread = aggregate['unread'] or 0
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


def _status_counts(queryset):
    """`(status_code, count)` pairs, deterministically ordered.

    `.order_by()` clears the model's default ordering on purpose: Django adds
    ordering columns to `GROUP BY`, so `Task.Meta.ordering` would silently turn
    this into a per-(status, due_date, created_at) distribution — more rows to
    group, and a token sensitive to columns it never meant to describe.
    """
    return tuple(
        sorted(
            queryset.order_by()
            .values_list('status__code')
            .annotate(count=Count('pk', distinct=True))
            .values_list('status__code', 'count')
        )
    )


def _tasks_revision(user):
    from tasks.permissions import get_readable_tasks_queryset

    visible = get_readable_tasks_queryset(user)
    aggregate = visible.aggregate(
        total=Count('pk', distinct=True),
        last_created=Max('created_at'),
        last_updated=Max('updated_at'),
        last_completed=Max('completed_at'),
        assignments=Count('assignees', distinct=True),
        # Assignment fingerprint: the count alone cannot tell "swap assignee A
        # for assignee B" from "nothing happened". The highest assignment id
        # moves whenever a row is added, so a replacement changes the token
        # even though the number of assignees is unchanged — and it costs the
        # database one more aggregate over a join it is already doing, rather
        # than a list of assignee ids loaded into Python.
        last_assignment=Max('assignees__pk'),
    )
    # Status mix, so completing a task changes the token even when the row
    # count and the timestamps happen to look the same.
    statuses = _status_counts(visible)
    return _token(
        't',
        aggregate['total'],
        aggregate['last_created'],
        aggregate['last_updated'],
        aggregate['last_completed'],
        aggregate['assignments'],
        aggregate['last_assignment'],
        statuses,
    )


ARCHIVED = Q(status__code='ARCHIVED')


def _visible_acts(user):
    """Every globally readable act, active and archived, as one queryset.

    Deliberately the shared readable queryset rather than two separate
    ones: it is used both for aggregates here and as a *subquery* for comments
    and history, so no act identifier is ever loaded into Python.
    """
    from acts.permissions import get_all_visible_acts_queryset

    return get_all_visible_acts_queryset(user)


def _acts_revision(user):
    visible = _visible_acts(user)
    # One query for both halves: filtered aggregates split active from archived
    # without a second round trip and without loading a single row.
    aggregate = visible.aggregate(
        active_total=Count('pk', filter=~ARCHIVED, distinct=True),
        active_last=Max('updated_at', filter=~ARCHIVED),
        archived_total=Count('pk', filter=ARCHIVED, distinct=True),
        archived_last=Max('updated_at', filter=ARCHIVED),
    )
    statuses = _status_counts(visible.exclude(status__code='ARCHIVED'))
    return _token(
        'a',
        aggregate['active_total'],
        aggregate['active_last'],
        aggregate['archived_total'],
        aggregate['archived_last'],
        statuses,
    )


def _comments_revision(user):
    """Comments and history inside the acts this user may see.

    The visible-acts queryset is passed to the database as a subquery
    (`act__in=<queryset>`), never resolved into a list of primary keys first:
    PostgreSQL aggregates the whole thing itself, and the cost stops growing
    with how many acts the user can see. Portable ORM only, so this still runs
    on SQLite for the ordinary test suite.
    """
    from acts.models import ActComment, ActHistoryEvent

    visible = _visible_acts(user)
    comments = ActComment.objects.filter(act__in=visible).aggregate(
        total=Count('pk'), last=Max('created_at')
    )
    history = ActHistoryEvent.objects.filter(act__in=visible).aggregate(
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
    from tasks.permissions import get_readable_tasks_queryset

    linked = get_readable_tasks_queryset(user).filter(act__isnull=False)
    aggregate = linked.aggregate(
        total=Count('pk', distinct=True),
        last_updated=Max('updated_at'),
        last_completed=Max('completed_at'),
        # Same assignment fingerprint as the tasks revision: a replaced
        # assignee must move this token too, since the act detail lists them.
        assignments=Count('assignees', distinct=True),
        last_assignment=Max('assignees__pk'),
    )
    statuses = _status_counts(linked)
    return _token(
        'v',
        aggregate['total'],
        aggregate['last_updated'],
        aggregate['last_completed'],
        aggregate['assignments'],
        aggregate['last_assignment'],
        statuses,
    )


def _workup_revision(user):
    """Calculator → «Проработка», which every authenticated user sees whole.

    The journal is one shared table with no per-user visibility rule, so the
    token takes no `user` filter — the argument is there only to keep every
    revision function the same shape. One aggregate, no rows loaded: a deleted
    row moves `total`, an edited one moves `last_updated`, and confirming or
    reopening production moves `confirmed` even when neither of the other two
    happens to change.
    """
    from calculator.models import WindingEntry

    aggregate = WindingEntry.objects.aggregate(
        total=Count('pk'),
        confirmed=Count('pk', filter=Q(production_confirmed=True)),
        last_updated=Max('updated_at'),
    )
    return _token(
        'w',
        aggregate['total'],
        aggregate['confirmed'],
        aggregate['last_updated'],
    )


def build_sync_state(user):
    """Return the user's current revision snapshot.

    A fixed, small number of aggregate queries — it never loads rows and never
    materialises identifiers, so the cost does not grow with how much data the
    user can see. The wire format (schema version, revision keys, counters) is
    part of the client contract and must not change here.
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
            REVISION_WORKUP: _workup_revision(user),
        },
        'unread_notifications': unread,
    }
