from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Act, ActAttachment, ActComment, ActHistoryEvent, get_act_status
from .permissions import (
    can_apply_ko_decision,
    can_apply_to_analysis,
    can_close_act,
    can_delete_attachment,
    can_send_to_ko,
    can_view_act,
    is_act_admin,
    get_user_role,
    get_visible_acts_queryset,
)


class ActWorkflowError(Exception):
    pass


def send_to_ko(act, user):
    if not can_send_to_ko(act, user):
        raise ActWorkflowError('Передача акта в КО недоступна для вашей роли или текущего статуса.')
    _require_status(act, 'CREATED_OTK')
    from_status = act.status
    to_status = _get_required_status('KO_REVIEW')
    act.status = to_status
    act.save(update_fields=['status', 'updated_at'])
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.SENT_TO_KO,
        'Акт передан в КО для рассмотрения.',
        from_status=from_status,
        to_status=to_status,
    )
    return act


def apply_ko_decision(act, user, decision, comment):
    if not can_apply_ko_decision(act, user):
        raise ActWorkflowError('Решение КО недоступно для вашей роли или текущего статуса.')
    _require_status(act, 'KO_REVIEW')
    if decision not in Act.KoDecision.values:
        raise ActWorkflowError('Недопустимое решение КО.')

    from_status = act.status
    next_status_code = 'CREATED_OTK' if decision == Act.KoDecision.RETURN else 'TO_ANALYSIS'
    to_status = _get_required_status(next_status_code)
    act.ko_decision = decision
    act.ko_comment = comment
    act.ko_decision_by = user
    act.ko_decision_at = timezone.now()
    act.status = to_status
    act.save(
        update_fields=[
            'ko_decision',
            'ko_comment',
            'ko_decision_by',
            'ko_decision_at',
            'status',
            'updated_at',
        ]
    )
    if decision == Act.KoDecision.RETURN:
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_OTK,
            'Акт возвращён в ОТК на уточнение.',
            from_status=from_status,
            to_status=to_status,
        )
    else:
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.KO_DECISION_APPLIED,
            f'Решение КО внесено: {act.get_ko_decision_display()}.',
            from_status=from_status,
            to_status=to_status,
        )
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.SENT_TO_TO,
            'Акт передан в ТО для анализа.',
            from_status=from_status,
            to_status=to_status,
        )
    return act


def apply_to_analysis(act, user, root_cause, action_summary):
    if not can_apply_to_analysis(act, user):
        raise ActWorkflowError('Анализ ТО недоступен для вашей роли или текущего статуса.')
    _require_status(act, 'TO_ANALYSIS')
    from_status = act.status
    to_status = _get_required_status('ACTIONS_ASSIGNED')
    act.to_root_cause = root_cause
    act.to_action_summary = action_summary
    act.to_analysis_by = user
    act.to_analysis_at = timezone.now()
    act.status = to_status
    act.save(
        update_fields=[
            'to_root_cause',
            'to_action_summary',
            'to_analysis_by',
            'to_analysis_at',
            'status',
            'updated_at',
        ]
    )
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.TO_ANALYSIS_APPLIED,
        'Анализ ТО внесён, мероприятия ожидают дальнейшей проработки.',
        from_status=from_status,
        to_status=to_status,
    )
    return act


def add_act_history_event(act, user, event_type, message, from_status=None, to_status=None):
    return ActHistoryEvent.objects.create(
        act=act,
        user=user if getattr(user, 'is_authenticated', False) else None,
        event_type=event_type,
        message=message,
        from_status=from_status,
        to_status=to_status,
    )


def add_act_comment(act, user, text):
    comment = ActComment.objects.create(
        act=act,
        author=user if getattr(user, 'is_authenticated', False) else None,
        text=text,
    )
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.COMMENT_ADDED,
        'Комментарий добавлен пользователем.',
    )
    return comment


def add_act_attachment(act, user, uploaded_file, description=''):
    attachment = ActAttachment.objects.create(
        act=act,
        uploaded_by=user if getattr(user, 'is_authenticated', False) else None,
        file=uploaded_file,
        original_name=uploaded_file.name,
        description=description,
        file_size=getattr(uploaded_file, 'size', 0) or 0,
        content_type=getattr(uploaded_file, 'content_type', '') or '',
    )
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.ATTACHMENT_ADDED,
        f'Вложение добавлено: {attachment.original_name}.',
    )
    return attachment


def delete_act_attachment(attachment, user):
    if not can_delete_attachment(attachment, user):
        raise ActWorkflowError('Удаление вложения недоступно для вашей роли.')

    act = attachment.act
    original_name = attachment.original_name
    file_field = attachment.file
    attachment.delete()
    if file_field:
        try:
            file_field.delete(save=False)
        except OSError:
            pass
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.ATTACHMENT_DELETED,
        f'Вложение удалено: {original_name}.',
    )


def format_file_size(size_bytes):
    if size_bytes < 1024:
        return f'{size_bytes} Б'
    if size_bytes < 1024 * 1024:
        return f'{size_bytes / 1024:.1f} КБ'
    return f'{size_bytes / (1024 * 1024):.1f} МБ'


def validate_act_can_be_closed(act):
    if _status_code(act) != 'ACTIONS_ASSIGNED':
        raise ActWorkflowError('Акт можно закрыть только после назначения мероприятий.')
    required_fields = (
        (act.ko_decision, 'Перед закрытием нужно внести решение КО.'),
        (act.ko_decision_by_id, 'Перед закрытием должен быть указан автор решения КО.'),
        (act.ko_decision_at, 'Перед закрытием должна быть указана дата решения КО.'),
        (act.to_root_cause, 'Перед закрытием нужно заполнить корневую причину ТО.'),
        (act.to_action_summary, 'Перед закрытием нужно заполнить мероприятия ТО.'),
        (act.to_analysis_by_id, 'Перед закрытием должен быть указан автор анализа ТО.'),
        (act.to_analysis_at, 'Перед закрытием должна быть указана дата анализа ТО.'),
    )
    for value, message in required_fields:
        if not value:
            raise ActWorkflowError(message)


def close_act(act, user, closing_comment=''):
    if not can_close_act(act, user):
        raise ActWorkflowError('Закрытие акта недоступно для вашей роли или текущего статуса.')
    validate_act_can_be_closed(act)
    from_status = act.status
    to_status = _get_required_status('CLOSED')
    act.status = to_status
    act.closed_by = user
    act.closed_at = timezone.now()
    act.closing_comment = closing_comment
    act.save(
        update_fields=[
            'status',
            'closed_by',
            'closed_at',
            'closing_comment',
            'updated_at',
        ]
    )
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.ACT_CLOSED,
        'Акт закрыт.',
        from_status=from_status,
        to_status=to_status,
    )
    return act


def get_available_act_actions(act, user):
    return {
        'send_to_ko': can_send_to_ko(act, user),
        'ko_decision': can_apply_ko_decision(act, user),
        'to_analysis': can_apply_to_analysis(act, user),
        'close_act': can_close_act(act, user),
        'print_act': can_view_act(act, user),
    }


def get_visible_acts_for_user(user):
    return get_visible_acts_queryset(user)


def get_role_context_text(user):
    if is_act_admin(user):
        return (
            'Администратор: показаны все акты на всех этапах. Доступны все действия, '
            'разрешённые текущим статусом акта.'
        )
    role = get_user_role(user)
    if role == 'otk':
        return 'Показаны только акты, созданные вами и находящиеся на этапе ОТК.'
    if role == 'ko':
        return 'Показаны только акты, находящиеся на рассмотрении КО.'
    if role == 'to':
        return 'Показаны только акты, находящиеся на анализе ТО.'
    if role == 'manager':
        return 'Показаны все акты.'
    return 'Для пользователя без роли список актов недоступен.'


def _require_status(act, expected_code):
    actual_code = getattr(getattr(act, 'status', None), 'code', '')
    if actual_code != expected_code:
        raise ActWorkflowError('Акт находится в неподходящем статусе для этого действия.')


def _get_required_status(code):
    try:
        return get_act_status(code)
    except ValidationError as exc:
        message = exc.messages[0] if getattr(exc, 'messages', None) else str(exc)
        raise ActWorkflowError(message) from exc
