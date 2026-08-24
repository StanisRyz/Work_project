"""Event factories.

Business services never assemble event dictionaries by hand: they call one of
these factories, which is what keeps the payload minimal and uniform.

Everything here follows one rule — identifiers and safe technical metadata
only. No comment text, no defect descriptions, no email addresses, no file
names, no authorisation data, no password hashes, no permissions, no whole
models. A client that needs content refetches it through the normal endpoints.
"""

from .events import (
    RESOURCE_ACT,
    RESOURCE_COMMENT,
    RESOURCE_NOTIFICATION,
    RESOURCE_PROTOCOL,
    RESOURCE_TASK,
    RESOURCE_USER,
    RESOURCE_WORKUP,
    RealtimeEvent,
    RealtimeEventType,
)


# Scope of a read operation covering the recipient's whole history.
READ_SCOPE_ALL = 'all'

# Above this many rows the event reports only counts: a client that needs the
# exact list refetches it through the ordinary notifications endpoints.
NOTIFICATION_READ_MAX_IDS = 20


def _status_code(instance, attribute='status'):
    return getattr(getattr(instance, attribute, None), 'code', '') or ''


def notification_created_event(notification):
    """Identifiers only, whatever the notification is about.

    A notification can now be sourced from an act, a protocol or a task, so the
    payload carries `source_type` and the three nullable ids — `act_id` keeps
    its name and stays NULL for the other sources. No title, message, comment,
    protocol content, task text, name or address ever travels this way: a
    client that needs text refetches it through the notifications endpoints,
    authenticated as itself.
    """
    return RealtimeEvent(
        event_type=RealtimeEventType.NOTIFICATION_CREATED,
        resource_type=RESOURCE_NOTIFICATION,
        resource_id=notification.pk,
        data={
            'recipient_id': notification.recipient_id,
            'actor_id': notification.actor_id,
            'source_type': str(notification.source_type),
            'act_id': notification.related_act_id,
            'protocol_id': notification.related_protocol_id,
            'task_id': notification.related_task_id,
            'notification_event_type': str(notification.event_type),
        },
    )


def notification_read_event(user_id, notification_ids, unread_count, scope):
    """One aggregated read event per user per operation.

    Reading is a per-user state change that can cover many rows at once, so the
    resource is always the user. `changed_count`, `unread_count` and `scope`
    are always present; the explicit id list is included only when the
    operation is small enough for it to stay bounded — «отметить все» over a
    long history must not produce an unbounded payload.
    """
    identifiers = sorted(int(pk) for pk in notification_ids)
    data = {
        'changed_count': len(identifiers),
        'unread_count': int(unread_count),
        'scope': str(scope),
    }
    if scope != READ_SCOPE_ALL and len(identifiers) <= NOTIFICATION_READ_MAX_IDS:
        data['notification_ids'] = identifiers
    return RealtimeEvent(
        event_type=RealtimeEventType.NOTIFICATION_READ,
        resource_type=RESOURCE_USER,
        resource_id=user_id,
        data=data,
    )


def task_created_event(task, assignee_ids):
    return RealtimeEvent(
        event_type=RealtimeEventType.TASK_CREATED,
        resource_type=RESOURCE_TASK,
        resource_id=task.pk,
        data={
            'act_id': task.act_id,
            'source_action_id': task.source_action_id,
            'status_code': _status_code(task),
            'assignee_count': len(assignee_ids),
        },
    )


def task_updated_event(task, changed_fields=()):
    return RealtimeEvent(
        event_type=RealtimeEventType.TASK_UPDATED,
        resource_type=RESOURCE_TASK,
        resource_id=task.pk,
        data={
            'act_id': task.act_id,
            'status_code': _status_code(task),
            'changed_fields': sorted(str(name) for name in changed_fields),
        },
    )


def task_completed_event(task):
    return RealtimeEvent(
        event_type=RealtimeEventType.TASK_COMPLETED,
        resource_type=RESOURCE_TASK,
        resource_id=task.pk,
        data={
            'act_id': task.act_id,
            'status_code': _status_code(task),
            'completed_by_id': task.completed_by_id,
        },
    )


def act_created_event(act):
    """A newly created act. Identifiers and status only — never party data."""
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_CREATED,
        resource_type=RESOURCE_ACT,
        resource_id=act.pk,
        data={
            'status_code': _status_code(act),
            'author_id': act.created_by_id,
        },
    )


def act_updated_event(act):
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_UPDATED,
        resource_type=RESOURCE_ACT,
        resource_id=act.pk,
        data={'status_code': _status_code(act)},
    )


def act_status_changed_event(act, history_event):
    """One event type for every transition — never one type per status."""
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_STATUS_CHANGED,
        resource_type=RESOURCE_ACT,
        resource_id=act.pk,
        data={
            'from_status_code': _status_code(history_event, 'from_status'),
            'to_status_code': _status_code(history_event, 'to_status'),
            'history_event_id': history_event.pk,
            'actor_id': history_event.user_id,
        },
    )


# --------------------------------------------------------------------------
# Protocols
#
# A protocol status is a plain `TextChoices` value on the row, not a related
# reference table, so it travels as its own code rather than through
# `_status_code()`. Everything else follows the act factories exactly:
# identifiers, status codes and the revision number — never «Повестка»,
# «Слушали», a decision text, a participant name or a return comment.
# --------------------------------------------------------------------------


def protocol_created_event(protocol):
    return RealtimeEvent(
        event_type=RealtimeEventType.PROTOCOL_CREATED,
        resource_type=RESOURCE_PROTOCOL,
        resource_id=protocol.pk,
        data={
            'status': str(protocol.status),
            'author_id': protocol.author_id,
            'protocol_type_id': protocol.protocol_type_id,
        },
    )


def protocol_updated_event(protocol):
    """The document content changed while the protocol stayed where it was."""
    return RealtimeEvent(
        event_type=RealtimeEventType.PROTOCOL_UPDATED,
        resource_type=RESOURCE_PROTOCOL,
        resource_id=protocol.pk,
        data={
            'status': str(protocol.status),
            'revision': int(protocol.revision),
        },
    )


def protocol_deleted_event(protocol_id):
    """A deleted draft: only the identifier survives, the row itself is gone."""
    return RealtimeEvent(
        event_type=RealtimeEventType.PROTOCOL_DELETED,
        resource_type=RESOURCE_PROTOCOL,
        resource_id=int(protocol_id),
    )


def protocol_status_changed_event(protocol, from_status):
    """One event type for every transition — never one type per status.

    `from_status` is the status the protocol was observed in *before* the
    transaction started; `to_status` is where it actually ended up. A
    submission that required nobody and archived itself in the same
    transaction is therefore one `DRAFT → ARCHIVED` event, not a pair
    describing an `APPROVAL` state no user could ever see.
    """
    return RealtimeEvent(
        event_type=RealtimeEventType.PROTOCOL_STATUS_CHANGED,
        resource_type=RESOURCE_PROTOCOL,
        resource_id=protocol.pk,
        data={
            'from_status': str(from_status or ''),
            'to_status': str(protocol.status),
            'revision': int(protocol.revision),
        },
    )


def protocol_approval_changed_event(protocol, approval):
    """One person's decision on one revision. The resource is the protocol.

    The client refreshes protocol blocks, so keying the event on the protocol
    is what lets an open page filter by `resource_id` without a second lookup.
    The return comment is deliberately absent: it is business text and stays
    behind the ordinary permission-checked fragment endpoints.
    """
    return RealtimeEvent(
        event_type=RealtimeEventType.PROTOCOL_APPROVAL_CHANGED,
        resource_type=RESOURCE_PROTOCOL,
        resource_id=protocol.pk,
        data={
            'approval_id': approval.pk,
            'approval_status': str(approval.status),
            'revision': int(approval.revision),
            'status': str(protocol.status),
            'actor_id': approval.user_id,
        },
    )


def comment_created_event(comment):
    return RealtimeEvent(
        event_type=RealtimeEventType.COMMENT_CREATED,
        resource_type=RESOURCE_COMMENT,
        resource_id=comment.pk,
        data={
            'act_id': comment.act_id,
            'author_id': comment.author_id,
        },
    )


def workup_created_event(entry):
    """A new «Проработка» row. Its identifier and how it was born — no numbers.

    Nothing of the journal itself travels here: geometry, production time, the
    1С expression and the employee name stay behind the ordinary
    `calculator:entry_list` endpoint the client refetches.
    """
    return RealtimeEvent(
        event_type=RealtimeEventType.WORKUP_CREATED,
        resource_type=RESOURCE_WORKUP,
        resource_id=entry.pk,
        data={'source': str(entry.source)},
    )


def workup_updated_event(entry, change):
    """One row changed. `change` says which state moved, never to what value."""
    return RealtimeEvent(
        event_type=RealtimeEventType.WORKUP_UPDATED,
        resource_type=RESOURCE_WORKUP,
        resource_id=entry.pk,
        data={
            'change': str(change),
            'production_confirmed': bool(entry.production_confirmed),
        },
    )


def workup_deleted_event(entry_id):
    """A removed row: only the identifier survives, the row itself is gone."""
    return RealtimeEvent(
        event_type=RealtimeEventType.WORKUP_DELETED,
        resource_type=RESOURCE_WORKUP,
        resource_id=int(entry_id),
    )
