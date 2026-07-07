from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import Act, get_act_status
from .permissions import (
    can_apply_ko_decision,
    can_apply_to_analysis,
    can_send_to_ko,
    get_user_role,
    get_visible_acts_queryset,
)


class ActWorkflowError(Exception):
    pass


def send_to_ko(act, user):
    if not can_send_to_ko(act, user):
        raise ActWorkflowError('Передача акта в КО недоступна для вашей роли или текущего статуса.')
    _require_status(act, 'CREATED_OTK')
    act.status = _get_required_status('KO_REVIEW')
    act.save(update_fields=['status', 'updated_at'])
    return act


def apply_ko_decision(act, user, decision, comment):
    if not can_apply_ko_decision(act, user):
        raise ActWorkflowError('Решение КО недоступно для вашей роли или текущего статуса.')
    _require_status(act, 'KO_REVIEW')
    if decision not in Act.KoDecision.values:
        raise ActWorkflowError('Недопустимое решение КО.')

    next_status_code = 'CREATED_OTK' if decision == Act.KoDecision.RETURN else 'TO_ANALYSIS'
    act.ko_decision = decision
    act.ko_comment = comment
    act.ko_decision_by = user
    act.ko_decision_at = timezone.now()
    act.status = _get_required_status(next_status_code)
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
    return act


def apply_to_analysis(act, user, root_cause, action_summary):
    if not can_apply_to_analysis(act, user):
        raise ActWorkflowError('Анализ ТО недоступен для вашей роли или текущего статуса.')
    _require_status(act, 'TO_ANALYSIS')
    act.to_root_cause = root_cause
    act.to_action_summary = action_summary
    act.to_analysis_by = user
    act.to_analysis_at = timezone.now()
    act.status = _get_required_status('ACTIONS_ASSIGNED')
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
    return act


def get_available_act_actions(act, user):
    return {
        'send_to_ko': can_send_to_ko(act, user),
        'ko_decision': can_apply_ko_decision(act, user),
        'to_analysis': can_apply_to_analysis(act, user),
    }


def get_visible_acts_for_user(user):
    return get_visible_acts_queryset(user)


def get_role_context_text(user):
    role = get_user_role(user)
    if role == 'otk':
        return 'Показаны акты, созданные вами.'
    if role == 'ko':
        return 'Показаны акты на этапе решения КО и обработанные вами акты.'
    if role == 'to':
        return 'Показаны акты на этапе анализа ТО, мероприятий и обработанные вами акты.'
    if role in {'manager', 'admin'}:
        return 'Показаны все акты.'
    return 'Для пользователя без роли список актов ограничен.'


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
