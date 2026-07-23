from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Act, ActAttachment, ActComment, ActCorrectiveAction, ActHistoryEvent, ActRootAnalysis, get_act_status
from .permissions import (
    can_apply_ko_decision,
    can_apply_to_analysis,
    can_close_act,
    can_delete_attachment,
    can_edit_act,
    can_return_to_otk,
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


def apply_ko_decision(act, user, defect_decisions):
    if not can_apply_ko_decision(act, user):
        raise ActWorkflowError('Решение КО недоступно для вашей роли или текущего статуса.')
    _require_status(act, 'KO_REVIEW')
    defect_decisions = list(defect_decisions)
    defects = list(act.defects.select_related('defect_type'))
    if defects:
        expected_ids = {defect.pk for defect in defects}
        received_ids = {defect.pk for defect, _decision, _comment in defect_decisions}
        if expected_ids != received_ids:
            raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
    elif len(defect_decisions) != 1 or defect_decisions[0][0] is not None:
        raise ActWorkflowError('Необходимо внести решение КО по каждому дефекту.')
    for _defect, decision, _comment in defect_decisions:
        if decision not in Act.KoDecision.new_values():
            raise ActWorkflowError('Недопустимое решение КО.')

    from_status = act.status
    to_status = _get_required_status('TO_ANALYSIS')
    first_defect, first_decision, first_comment = defect_decisions[0]
    act.ko_decision = first_decision
    act.ko_comment = first_comment
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
    for defect, decision, comment in defect_decisions:
        if defect is not None:
            defect.ko_decision = decision
            defect.ko_comment = comment
            defect.ko_decision_by = user
            defect.ko_decision_at = act.ko_decision_at
            defect.save(update_fields=['ko_decision', 'ko_comment', 'ko_decision_by', 'ko_decision_at', 'updated_at'])
            message = f'Решение КО по дефекту «{defect.defect_type}»: {defect.get_ko_decision_display()}.'
        else:
            message = f'Решение КО внесено: {act.get_ko_decision_display()}.'
        add_act_history_event(act, user, ActHistoryEvent.EventType.KO_DECISION_APPLIED, message)
    add_act_history_event(
        act,
        user,
        ActHistoryEvent.EventType.SENT_TO_TO,
        'Акт передан в ТО для анализа.',
        from_status=from_status,
        to_status=to_status,
    )
    return act


def return_to_otk(act, user, return_comment):
    return_comment = (return_comment or '').strip()
    if not return_comment:
        raise ActWorkflowError('Укажите комментарий к возврату.')
    if not can_return_to_otk(act, user):
        raise ActWorkflowError('Возврат акта в ОТК недоступен для вашей роли или текущего статуса.')
    _require_status(act, 'KO_REVIEW')
    with transaction.atomic():
        add_act_comment(act, user, return_comment)
        from_status = act.status
        to_status = _get_required_status('CREATED_OTK')
        act.status = to_status
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            user,
            ActHistoryEvent.EventType.RETURNED_TO_OTK,
            'Акт возвращён в ОТК на доработку.',
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


def apply_structured_to_analysis(act, user, analysis_data):
    with transaction.atomic():
        if not can_apply_to_analysis(act, user):
            raise ActWorkflowError('Анализ ТО недоступен для вашей роли или текущего статуса.')
        _require_status(act, 'TO_ANALYSIS')
        if not analysis_data or any(not item['actions'] for item in analysis_data):
            raise ActWorkflowError('Добавьте корневую причину и корректирующее мероприятие.')
        from_status = act.status
        to_status = _get_required_status('ACTIONS_ASSIGNED')
        ActRootAnalysis.objects.filter(act=act).delete()
        for root_index, root_data in enumerate(analysis_data):
            root_analysis = ActRootAnalysis.objects.create(
                act=act,
                root_cause=root_data['root_cause'],
                display_order=root_index,
            )
            for action_index, action_data in enumerate(root_data['actions']):
                ActCorrectiveAction.objects.create(
                    root_analysis=root_analysis,
                    comment=action_data['comment'],
                    department=action_data['department'],
                    responsible=action_data['responsible'],
                    due_date=action_data['due_date'],
                    display_order=action_index,
                )

        first_root = analysis_data[0]
        first_action = first_root['actions'][0]
        act.to_root_cause = first_root['root_cause']
        act.to_action_summary = first_action['comment']
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


def clear_all_acts():
    """Delete every act and its database records, then remove attached files."""
    attachments = list(ActAttachment.objects.exclude(file='').only('file'))
    with transaction.atomic():
        deleted_count = Act.objects.count()
        Act.objects.all().delete()
    for attachment in attachments:
        try:
            attachment.file.delete(save=False)
        except OSError:
            pass
    return deleted_count


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
        'edit_act': can_edit_act(act, user),
        'send_to_ko': can_send_to_ko(act, user),
        'ko_decision': can_apply_ko_decision(act, user),
        'return_to_otk': can_return_to_otk(act, user),
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
