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
    RESOURCE_TASK,
    RESOURCE_USER,
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
    return RealtimeEvent(
        event_type=RealtimeEventType.NOTIFICATION_CREATED,
        resource_type=RESOURCE_NOTIFICATION,
        resource_id=notification.pk,
        data={
            'recipient_id': notification.recipient_id,
            'actor_id': notification.actor_id,
            'act_id': notification.related_act_id,
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
