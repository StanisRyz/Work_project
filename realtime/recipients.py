"""Who receives an event.

Every function here reuses the *existing* internal-notification routing rules —
this module adds no new business routing of its own. Outsiders are excluded by
construction: only users the current rules already address are returned, and
inactive users and inactive profiles are dropped exactly as
`notifications.services.create_notifications` drops them.
"""

from django.contrib.auth import get_user_model

from .targets import act_target, user_target, user_targets


def _active_ids(user_ids):
    """Keep only ids of active users with an active profile, deterministically."""
    ids = {user_id for user_id in user_ids if user_id}
    if not ids:
        return []
    return list(
        get_user_model()
        .objects.filter(pk__in=ids, is_active=True, userprofile__is_active=True)
        .order_by('pk')
        .values_list('pk', flat=True)
    )


def _ids_of(users):
    return [getattr(user, 'pk', None) for user in users]


def notification_targets(notification):
    """A notification is private to its recipient — no act-wide target."""
    return [user_target(notification.recipient_id)]


def notification_read_targets(user_or_id):
    """Read state belongs to one user only."""
    return [user_target(user_or_id)]


def task_targets(task):
    """The task's current active assignees, plus the act routing hint."""
    assignee_ids = get_user_model().objects.filter(
        task_assignments__task=task,
        is_active=True,
        userprofile__is_active=True,
    ).order_by('pk').values_list('pk', flat=True)
    return [*user_targets(assignee_ids), act_target(task.act_id)]


def act_created_targets(act):
    """Who may already see a brand-new act.

    Its author plus every active user with full act access (managers,
    administrators and superusers). KO and TO are deliberately excluded: at
    `CREATED_OTK` the current permissions do not let them see the act, so
    sending them the event would leak its existence. Duplicates, inactive users
    and inactive profiles are dropped by `_active_ids`.
    """
    from acts.permissions import has_full_act_access

    user_ids = [act.created_by_id]
    candidates = get_user_model().objects.filter(
        is_active=True, userprofile__is_active=True
    ).select_related('userprofile')
    user_ids += [user.pk for user in candidates if has_full_act_access(user)]
    return [*user_targets(_active_ids(user_ids)), act_target(act.pk)]


def act_targets(act):
    """Users the act already concerns: author, KO/TO authors, action assignees."""
    from notifications.services import get_act_participants

    return [
        *user_targets(_active_ids(_ids_of(get_act_participants(act)))),
        act_target(act.pk),
    ]


def act_status_changed_targets(act, history_event):
    """The transition's notification audience, plus the act's own participants.

    Both sets already exist in `notifications.services`; nothing new is routed
    here, so a user who would not be notified of the transition and has no
    relation to the act never becomes a recipient.
    """
    from notifications.services import get_act_participants, get_recipients_for_history_event

    user_ids = _ids_of(get_recipients_for_history_event(history_event))
    user_ids += _ids_of(get_act_participants(act))
    return [*user_targets(_active_ids(user_ids)), act_target(act.pk)]


def comment_targets(comment):
    """Exactly the participants the comment-notification routing addresses."""
    from notifications.services import get_comment_participants

    user_ids = _ids_of(get_comment_participants(comment.act))
    return [*user_targets(_active_ids(user_ids)), act_target(comment.act_id)]
