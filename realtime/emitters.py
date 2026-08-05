"""The API business services call.

Each function is the single place where one business fact becomes one event:
it builds the payload from a factory, resolves the audience from the existing
routing rules and registers publication for after the commit.

Every emitter returns early when real-time is disabled — *before* resolving
recipients — so the default configuration performs no extra queries and the
project behaves exactly as it did before this app existed.

These are called explicitly from services, never from `post_save` signals, so
loading a fixture, a technical `save()` or a half-built object graph can never
produce an event.
"""

from .factories import (
    act_created_event,
    act_status_changed_event,
    act_updated_event,
    comment_created_event,
    notification_created_event,
    notification_read_event,
    task_completed_event,
    task_created_event,
    task_updated_event,
)
from .publisher import publish_after_commit, realtime_enabled
from .recipients import (
    act_created_targets,
    act_status_changed_targets,
    act_targets,
    comment_targets,
    notification_read_targets,
    notification_targets,
    task_targets,
)


def emit_notification_created(notification):
    """One created Notification row → at most one event.

    Call sites must only pass genuinely created rows: a deduplicated
    `get_or_create` hit must not reach this function.
    """
    if not realtime_enabled():
        return None
    event = notification_created_event(notification)
    publish_after_commit(event, notification_targets(notification))
    return event


def emit_notification_read(user, notification_ids, unread_count, scope):
    """One aggregated event per user per read operation.

    Nothing is emitted when no row actually changed, so a repeated click on an
    already-read notification stays silent.
    """
    if not realtime_enabled():
        return None
    notification_ids = list(notification_ids)
    if not notification_ids:
        return None
    event = notification_read_event(
        user_id=getattr(user, 'pk', user),
        notification_ids=notification_ids,
        unread_count=unread_count,
        scope=scope,
    )
    publish_after_commit(event, notification_read_targets(user))
    return event


def emit_task_created(task, assignee_ids):
    """Call only after the task *and* all its assignees are saved."""
    if not realtime_enabled():
        return None
    event = task_created_event(task, assignee_ids)
    publish_after_commit(event, task_targets(task))
    return event


def emit_task_updated(task, changed_fields=()):
    """Call after a significant change to an already-saved task.

    Currently emitted by `tasks.services.replace_task_assignees`. A future
    task-editing service must use this too, instead of inventing its own event.
    """
    if not realtime_enabled():
        return None
    event = task_updated_event(task, changed_fields)
    publish_after_commit(event, task_targets(task))
    return event


def emit_task_completed(task):
    if not realtime_enabled():
        return None
    event = task_completed_event(task)
    publish_after_commit(event, task_targets(task))
    return event


def emit_act_created(act):
    """Call only after the act and its defects are saved, inside the atomic
    block, so a rollback or a failed validation publishes nothing."""
    if not realtime_enabled():
        return None
    event = act_created_event(act)
    publish_after_commit(event, act_created_targets(act))
    return event


def emit_act_updated(act):
    if not realtime_enabled():
        return None
    event = act_updated_event(act)
    publish_after_commit(event, act_targets(act))
    return event


def emit_act_status_changed(act, history_event):
    """One event per successful transition, built on the locked act instance."""
    if not realtime_enabled():
        return None
    event = act_status_changed_event(act, history_event)
    publish_after_commit(event, act_status_changed_targets(act, history_event))
    return event


def emit_comment_created(comment):
    if not realtime_enabled():
        return None
    event = comment_created_event(comment)
    publish_after_commit(event, comment_targets(comment))
    return event
