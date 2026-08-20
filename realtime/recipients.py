"""Who receives an event.

Every function here reuses the *existing* internal-notification routing rules —
this module adds no new business routing of its own. Outsiders are excluded by
construction: only users the current rules already address are returned, and
inactive users and inactive profiles are dropped exactly as
`notifications.services.create_notifications` drops them.
"""

from django.contrib.auth import get_user_model
from django.db.models import Q

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
    """The task's current active assignees, plus the act routing hint.

    The assignees are the authoritative recipients; the act target is only a
    routing hint, and a task whose source is not an act simply has none — it is
    omitted rather than faked, since `act:None` is not a routable target.
    """
    assignee_ids = get_user_model().objects.filter(
        task_assignments__task=task,
        is_active=True,
        userprofile__is_active=True,
    ).order_by('pk').values_list('pk', flat=True)
    targets = list(user_targets(assignee_ids))
    if task.act_id:
        targets.append(act_target(task.act_id))
    return targets


def act_created_targets(act):
    """Who may already see a brand-new act.

    Its author plus every active user with full act access (managers,
    administrators and superusers). KO and TO are deliberately excluded: at
    `CREATED_OTK` the current permissions do not let them see the act, so
    sending them the event would leak its existence.

    The full-access set is resolved by the database
    (`acts.permissions.get_full_act_access_users_queryset`) instead of loading
    every active user and testing `has_full_act_access` in Python — the old
    shape made the cost of creating one act grow with the size of the whole
    user table. The rule itself is unchanged, and it stays defined in
    `acts.permissions`, not duplicated here.
    """
    from acts.permissions import get_full_act_access_users_queryset

    user_ids = [act.created_by_id]
    user_ids += list(get_full_act_access_users_queryset().values_list('pk', flat=True))
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


def workup_targets():
    """Everyone who may open Calculator → «Проработка».

    Reading the journal is open to every authenticated user — that is what
    `calculator.views.entry_list` allows — so the audience is every active
    account, and the event says nothing a reader could not already fetch from
    that endpoint. Resolved by the database rather than by loading users into
    Python, exactly like `act_created_targets`, so one journal write costs the
    same regardless of how large the user table is.

    Inactive users and inactive profiles are dropped as everywhere else; a
    genuine superuser is kept even without a profile, mirroring the
    profile-independent fallback in `calculator.permissions.can_manage_workup`.

    Deliberately still per-user `user:<id>` targets: the journal is shared, but
    the SSE stream a client may subscribe to is not, and no client-selectable
    public channel is introduced for it.
    """
    return user_targets(
        get_user_model()
        .objects.filter(Q(userprofile__is_active=True) | Q(is_superuser=True), is_active=True)
        .order_by('pk')
        .values_list('pk', flat=True)
    )


def comment_targets(comment):
    """Exactly the participants the comment-notification routing addresses."""
    from notifications.services import get_comment_participants

    user_ids = _ids_of(get_comment_participants(comment.act))
    return [*user_targets(_active_ids(user_ids)), act_target(comment.act_id)]
