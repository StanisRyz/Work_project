from dataclasses import dataclass
from urllib.parse import urljoin

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from realtime.emitters import emit_notification_created, emit_notification_read

from .models import Notification, NotificationDelivery


@dataclass(frozen=True)
class NotificationText:
    title: str
    message: str
    required_action: str


HISTORY_EVENT_TYPES = {
    'SENT_TO_KO': Notification.EventType.ACT_SENT_TO_KO,
    'SENT_TO_TO': Notification.EventType.ACT_SENT_TO_TO,
    'TO_ANALYSIS_APPLIED': Notification.EventType.ACT_SENT_TO_OTK,
    'RETURNED_TO_OTK': Notification.EventType.ACT_RETURNED_TO_OTK,
    'RETURNED_TO_KO': Notification.EventType.ACT_RETURNED_TO_KO,
    'RETURNED_TO_TO': Notification.EventType.ACT_RETURNED_TO_TO,
    'APPROVED': Notification.EventType.ACT_APPROVED,
}

# How many unread notifications the bell menu ever shows.
HEADER_NOTIFICATION_LIMIT = 5

# Every source relation is LEFT JOINed: selecting one is harmless for a
# notification that has none, and listing all three is what keeps a mixed page
# of act, protocol and task notifications free of per-row queries. The protocol
# type comes along because a protocol notification's label is built from it.
NOTIFICATION_SOURCE_SELECT_RELATED = (
    'actor',
    'related_act',
    'related_protocol__protocol_type',
    'related_task__protocol__protocol_type',
)

# Where each source type lives on the row, which route opens it, and how the
# «открыть» link is captioned. One table, so a new source type is one entry
# rather than a conditional in the model, the services and two templates.
SOURCE_FIELDS = {
    Notification.SourceType.ACT: 'related_act',
    Notification.SourceType.PROTOCOL: 'related_protocol',
    Notification.SourceType.TASK: 'related_task',
}

SOURCE_ROUTES = {
    Notification.SourceType.ACT: ('acts:detail', 'Открыть акт'),
    Notification.SourceType.PROTOCOL: ('protocols:detail', 'Открыть протокол'),
    Notification.SourceType.TASK: ('tasks:detail', 'Открыть задачу'),
}

# Email stays exactly as it was: act events only. Protocol events are in-app
# for now, and adding one here would start creating deliveries for them.
EMAIL_ELIGIBLE_EVENTS = {
    Notification.EventType.ACT_SENT_TO_KO,
    Notification.EventType.ACT_SENT_TO_TO,
    Notification.EventType.ACT_SENT_TO_OTK,
    Notification.EventType.ACT_RETURNED_TO_OTK,
    Notification.EventType.ACT_RETURNED_TO_KO,
    Notification.EventType.ACT_RETURNED_TO_TO,
    Notification.EventType.ACTION_ASSIGNED,
}


def get_recipients_for_history_event(history_event):
    """Return the users this history event notifies, using the routing table.

    Public so other modules (currently `realtime`) can address exactly the same
    audience instead of inventing a second routing rule. Returns an empty list
    for history events that are not notification-eligible.
    """
    event_type = HISTORY_EVENT_TYPES.get(history_event.event_type)
    if not event_type:
        return []
    return list(_recipients_for_event(event_type, history_event.act))


def notify_history_event(history_event):
    event_type = HISTORY_EVENT_TYPES.get(history_event.event_type)
    if not event_type:
        return []
    recipients = _recipients_for_event(event_type, history_event.act)
    return create_notifications(
        event_type=event_type,
        act=history_event.act,
        actor=history_event.user,
        recipients=recipients,
        source_key=f'history:{history_event.pk}',
    )


def notify_action_assigned(corrective_action, actor, assignees):
    act = corrective_action.root_analysis.act
    return create_notifications(
        event_type=Notification.EventType.ACTION_ASSIGNED,
        act=act,
        actor=actor,
        recipients=assignees,
        source_key=f'action:{corrective_action.pk}',
        exclude_actor=False,
    )


def notify_comment_added(comment, actor):
    return create_notifications(
        event_type=Notification.EventType.COMMENT_ADDED,
        act=comment.act,
        actor=actor,
        recipients=get_comment_participants(comment.act),
        source_key=f'comment:{comment.pk}',
    )


def notify_protocol_approval_required(protocol, approval, actor):
    """Tell one approver that a new round needs their decision.

    Keyed on the `ProtocolApproval` row, which is immutable and created fresh
    for every revision: a resubmission therefore notifies again, while calling
    this twice for the same round does not. This is the *only* notification an
    approver gets for that duty — the `PROTOCOL_APPROVAL` task it comes with is
    a work-queue entry and deliberately produces none of its own.
    """
    return create_notifications(
        event_type=Notification.EventType.PROTOCOL_APPROVAL_REQUIRED,
        protocol=protocol,
        actor=actor,
        recipients=[approval.user],
        source_key=f'approval:{approval.pk}',
    )


def notify_protocol_returned(protocol, actor):
    """Tell the author their protocol came back, without repeating the reason.

    The return comment stays in `ProtocolApproval.return_comment` and in the
    protocol history, which are the authoritative places to read it; a
    notification says only that the document needs attention.
    """
    return create_notifications(
        event_type=Notification.EventType.PROTOCOL_RETURNED_FOR_REVISION,
        protocol=protocol,
        actor=actor,
        recipients=[protocol.author],
        source_key=f'protocol:{protocol.pk}:revision:{protocol.revision}',
        exclude_actor=False,
    )


def notify_protocol_approved(protocol, actor):
    """Tell the author the protocol is fully approved and archived.

    `exclude_actor=False` is load-bearing: a protocol nobody had to approve is
    archived by its own author's submission, so actor and recipient are then
    legitimately the same person and the notification must still be created.
    """
    return create_notifications(
        event_type=Notification.EventType.PROTOCOL_APPROVED,
        protocol=protocol,
        actor=actor,
        recipients=[protocol.author],
        source_key=f'protocol:{protocol.pk}:revision:{protocol.revision}',
        exclude_actor=False,
    )


def notify_protocol_task_assigned(task, actor, assignees):
    """Tell the assignees of a real protocol task that it now exists.

    Task-sourced, so it links to the task itself rather than to the protocol.
    An approval queue entry is refused outright: it is not work anybody
    performs, and its approver already has a protocol notification.
    """
    from tasks.models import Task

    if task.source_type != Task.SourceType.PROTOCOL_ACTION:
        raise ValueError('Уведомление о назначении создаётся только для задачи по протоколу.')
    return create_notifications(
        event_type=Notification.EventType.PROTOCOL_TASK_ASSIGNED,
        task=task,
        actor=actor,
        recipients=assignees,
        source_key=f'task:{task.pk}',
        exclude_actor=False,
    )


def _resolve_source(act, protocol, task):
    """Exactly one source object, and the source type it implies.

    Resolving the type from the object it was given is what keeps
    `source_type` and the stored relation from ever disagreeing — nothing
    passes them separately.
    """
    given = [
        (source_type, source)
        for source_type, source in (
            (Notification.SourceType.ACT, act),
            (Notification.SourceType.PROTOCOL, protocol),
            (Notification.SourceType.TASK, task),
        )
        if source is not None
    ]
    if len(given) != 1:
        raise ValueError('Уведомление должно иметь ровно один источник: акт, протокол или задачу.')
    return given[0]


def create_notifications(
    *, event_type, actor, recipients, source_key,
    act=None, protocol=None, task=None, exclude_actor=True,
):
    """Create deduplicated in-app notifications and their independent email deliveries.

    Exactly one of `act`, `protocol` or `task` names what the notification is
    about; `source_type` follows from it.
    """
    source_type, source = _resolve_source(act, protocol, task)
    actor_id = getattr(actor, 'pk', None)
    recipient_ids = {
        recipient.pk
        for recipient in recipients
        if getattr(recipient, 'pk', None)
        and recipient.is_active
        and (not exclude_actor or recipient.pk != actor_id)
    }
    if not recipient_ids:
        return []

    users = get_user_model().objects.filter(
        pk__in=recipient_ids,
        is_active=True,
        userprofile__is_active=True,
    ).order_by('pk')
    text = _event_text(event_type, source_type, source)
    created_notifications = []
    with transaction.atomic():
        for recipient in users:
            notification, created = Notification.objects.get_or_create(
                recipient=recipient,
                deduplication_key=f'{event_type}:{source_key}',
                defaults={
                    'actor': actor if actor_id else None,
                    'event_type': event_type,
                    'title': text.title,
                    'message': text.message,
                    'source_type': source_type,
                    SOURCE_FIELDS[source_type]: source,
                },
            )
            if not created:
                # A deduplicated hit is not a new fact: no event for it.
                continue
            created_notifications.append(notification)
            if event_type in EMAIL_ELIGIBLE_EVENTS:
                _create_email_delivery(notification)
            # This is the single place an in-app notification comes into
            # existence, so it is the single place the event is emitted —
            # callers must never emit it again for the same row.
            emit_notification_created(notification)
    return created_notifications


def get_notification_header_state(user):
    """Everything the bell needs, resolved once.

    The single source for both the context processor (full page load) and the
    header fragment endpoint (real-time refresh), so the two can never drift
    apart and the ORM query is not repeated in several places. An anonymous
    user costs no query at all.
    """
    if not getattr(user, 'is_authenticated', False):
        return {'unread_count': 0, 'items': (), 'latest_notification_id': None}

    unread = Notification.objects.filter(recipient=user, is_read=False)
    items = list(
        unread.select_related(*NOTIFICATION_SOURCE_SELECT_RELATED)
        .order_by('-created_at', '-pk')[:HEADER_NOTIFICATION_LIMIT]
    )
    return {
        'unread_count': unread.count(),
        'items': items,
        'latest_notification_id': items[0].pk if items else None,
    }


def mark_notifications_read(user, *, scope, notification_ids=None):
    """Mark this user's unread notifications read and report what changed.

    The single entry point for every read action — one notification, the bell
    menu's shown items, or «отметить все». `notification_ids=None` means «all
    unread». Everything is scoped to `user`, so a foreign id can never be
    marked, and one operation emits at most one aggregated event.
    """
    unread = Notification.objects.filter(recipient=user, is_read=False)
    if notification_ids is not None:
        unread = unread.filter(pk__in=notification_ids)

    with transaction.atomic():
        changed_ids = list(unread.order_by('pk').values_list('pk', flat=True))
        if changed_ids:
            Notification.objects.filter(pk__in=changed_ids).update(
                is_read=True,
                read_at=timezone.now(),
            )
        unread_count = Notification.objects.filter(recipient=user, is_read=False).count()
        if changed_ids:
            # Nothing changed → no event, so a repeated click stays silent.
            emit_notification_read(user, changed_ids, unread_count, scope)
    return changed_ids, unread_count


def get_notification_source(notification):
    """The object a notification is about, chosen by its declared source type.

    Branching is on `source_type`, never on which relation happens to be
    filled: a NULL cannot tell an absent source from a wrong one.
    """
    return getattr(notification, SOURCE_FIELDS[notification.source_type])


def get_required_action(notification):
    return _event_text(
        notification.event_type,
        notification.source_type,
        get_notification_source(notification),
    ).required_action


def get_notification_url(notification, *, absolute=False):
    """The page this notification opens, resolved by source type.

    Built from the stored foreign key id, so rendering a link costs no query
    and no relation has to be loaded. Always a named route — never a
    hard-coded public path.
    """
    route, _label = SOURCE_ROUTES[notification.source_type]
    source_id = getattr(notification, f'{SOURCE_FIELDS[notification.source_type]}_id')
    path = reverse(route, kwargs={'pk': source_id})
    if not absolute:
        return path
    return urljoin(f"{settings.APP_BASE_URL.rstrip('/')}/", path.lstrip('/'))


def get_notification_open_label(notification):
    return SOURCE_ROUTES[notification.source_type][1]


def _create_email_delivery(notification):
    if not getattr(settings, 'EMAIL_NOTIFICATIONS_ENABLED', False):
        status = NotificationDelivery.Status.SKIPPED
        reason = 'Email-уведомления отключены настройкой EMAIL_NOTIFICATIONS_ENABLED.'
    elif not notification.recipient.email.strip():
        status = NotificationDelivery.Status.SKIPPED
        reason = 'У получателя не указан email-адрес.'
    else:
        status = NotificationDelivery.Status.PENDING
        reason = ''
    NotificationDelivery.objects.create(
        notification=notification,
        channel=NotificationDelivery.Channel.EMAIL,
        status=status,
        last_error=reason,
    )


def _recipients_for_event(event_type, act):
    if event_type == Notification.EventType.ACT_SENT_TO_KO:
        return _active_users_for_role(UserProfile.Role.KO)
    if event_type in {Notification.EventType.ACT_SENT_TO_TO, Notification.EventType.ACT_RETURNED_TO_TO}:
        return _active_users_for_role(UserProfile.Role.TO)
    if event_type in {Notification.EventType.ACT_SENT_TO_OTK, Notification.EventType.ACT_RETURNED_TO_OTK}:
        return [act.created_by]
    if event_type == Notification.EventType.ACT_RETURNED_TO_KO:
        return _active_users_for_role(UserProfile.Role.KO)
    if event_type == Notification.EventType.ACT_APPROVED:
        return get_act_participants(act)
    return []


def _active_users_for_role(role):
    return get_user_model().objects.select_related('userprofile').filter(
        is_active=True,
        userprofile__is_active=True,
        userprofile__role=role,
    )


def get_act_participants(act):
    user_ids = {
        user_id
        for user_id in (
            act.created_by_id,
            act.ko_decision_by_id,
            act.to_analysis_by_id,
        )
        if user_id
    }
    user_ids.update(
        get_user_model().objects.filter(
            actcorrectiveactionassignee__corrective_action__root_analysis__act=act,
        ).values_list('pk', flat=True)
    )
    return get_user_model().objects.select_related('userprofile').filter(pk__in=user_ids)


def get_comment_participants(act):
    from acts.permissions import can_contribute_to_act

    candidates = list(get_act_participants(act))
    status_code = getattr(act.status, 'code', '')
    if status_code == 'KO_REVIEW':
        candidates.extend(_active_users_for_role(UserProfile.Role.KO))
    elif status_code == 'TO_ANALYSIS':
        candidates.extend(_active_users_for_role(UserProfile.Role.TO))
    elif status_code in {'CREATED_OTK', 'OTK_REVIEW'}:
        candidates.append(act.created_by)
    return [user for user in candidates if can_contribute_to_act(act, user)]


def _protocol_label(protocol):
    return f'{protocol.protocol_type.name} №{protocol.number}'


def _protocol_event_text(event_type, protocol):
    label = _protocol_label(protocol)
    return {
        Notification.EventType.PROTOCOL_APPROVAL_REQUIRED: NotificationText(
            f'Требуется согласование протокола {label}',
            f'Протокол {label} ожидает вашего согласования.',
            'Откройте протокол и согласуйте его или верните на доработку.',
        ),
        Notification.EventType.PROTOCOL_RETURNED_FOR_REVISION: NotificationText(
            f'Протокол {label} возвращён на доработку',
            f'Протокол {label} возвращён на доработку согласующим.',
            'Откройте протокол, ознакомьтесь с причиной возврата и внесите исправления.',
        ),
        Notification.EventType.PROTOCOL_APPROVED: NotificationText(
            f'Протокол {label} согласован',
            f'Все требуемые согласования получены, протокол {label} помещён в архив.',
            'Дополнительных действий по протоколу не требуется.',
        ),
    }[event_type]


def _task_event_text(event_type, task):
    label = _protocol_label(task.protocol)
    return {
        Notification.EventType.PROTOCOL_TASK_ASSIGNED: NotificationText(
            f'Назначена задача по протоколу {label}',
            f'Вы назначены исполнителем задачи, созданной по протоколу {label}.',
            'Откройте задачу и выполните её в указанный срок.',
        ),
    }[event_type]


def _event_text(event_type, source_type, source):
    if source_type == Notification.SourceType.PROTOCOL:
        return _protocol_event_text(event_type, source)
    if source_type == Notification.SourceType.TASK:
        return _task_event_text(event_type, source)
    return _act_event_text(event_type, source)


def _act_event_text(event_type, act):
    number = act.number
    texts = {
        Notification.EventType.ACT_SENT_TO_KO: NotificationText(
            f'Акт {number} передан в КО',
            f'Акт {number} ожидает рассмотрения конструкторским отделом.',
            'Рассмотрите акт и внесите решение КО.',
        ),
        Notification.EventType.ACT_SENT_TO_TO: NotificationText(
            f'Акт {number} передан в ТО',
            f'По акту {number} принято решение КО, требуется анализ технологического отдела.',
            'Проведите анализ ТО и назначьте мероприятия.',
        ),
        Notification.EventType.ACT_SENT_TO_OTK: NotificationText(
            f'Акт {number} передан на проверку ОТК',
            f'Анализ ТО по акту {number} завершён и ожидает проверки ОТК.',
            'Проверьте анализ и утвердите акт либо верните его в ТО.',
        ),
        Notification.EventType.ACT_RETURNED_TO_OTK: NotificationText(
            f'Акт {number} возвращён в ОТК',
            f'Акт {number} возвращён в ОТК на доработку.',
            'Откройте акт, ознакомьтесь с комментарием возврата и внесите исправления.',
        ),
        Notification.EventType.ACT_RETURNED_TO_KO: NotificationText(
            f'Акт {number} возвращён в КО',
            f'Акт {number} возвращён в КО на доработку.',
            'Откройте акт, ознакомьтесь с комментарием возврата и уточните решение.',
        ),
        Notification.EventType.ACT_RETURNED_TO_TO: NotificationText(
            f'Акт {number} возвращён в ТО',
            f'Акт {number} возвращён в ТО на доработку.',
            'Откройте акт, ознакомьтесь с комментарием возврата и уточните анализ.',
        ),
        Notification.EventType.ACTION_ASSIGNED: NotificationText(
            f'Назначено мероприятие по акту {number}',
            f'Вы назначены исполнителем мероприятия по акту {number}.',
            'Ознакомьтесь с назначением в системе.',
        ),
        Notification.EventType.ACT_APPROVED: NotificationText(
            f'Акт {number} утверждён',
            f'Акт {number} утверждён и перемещён в архив.',
            'Дополнительных действий по акту не требуется.',
        ),
        Notification.EventType.COMMENT_ADDED: NotificationText(
            f'Новый комментарий к акту {number}',
            f'К акту {number} добавлен новый комментарий.',
            'Откройте акт, чтобы прочитать комментарий.',
        ),
    }
    return texts[event_type]
