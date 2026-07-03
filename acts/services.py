from django.db.models import Q

from .models import Act


def get_user_role(user):
    if not user.is_authenticated:
        return ''
    try:
        return user.userprofile.role
    except Exception:
        return ''


def is_manager_or_admin(user):
    return get_user_role(user) in {'manager', 'admin'}


def can_create_act(user):
    return get_user_role(user) in {'otk', 'manager', 'admin'}


def get_visible_acts(user):
    role = get_user_role(user)
    queryset = Act.objects.select_related(
        'created_by',
        'operation',
        'defect_type',
        'priority',
        'status',
    )
    if role in {'admin', 'manager'}:
        return queryset
    if role == 'otk':
        return queryset.filter(Q(created_by=user) | Q(status__code='CREATED_OTK'))
    if role == 'ko':
        return queryset.filter(status__code='KO_REVIEW')
    if role == 'to':
        return queryset.filter(status__code__in=['TO_ANALYSIS', 'ACTIONS_ASSIGNED'])
    return queryset.none()


def can_view_act_detail(user, act):
    role = get_user_role(user)
    if role in {'admin', 'manager'}:
        return True
    if role == 'otk':
        return act.created_by_id == user.id or act.status.code == 'CREATED_OTK'
    if role == 'ko':
        return act.status.code == 'KO_REVIEW' or act.ko_decision_by_id == user.id
    if role == 'to':
        return act.status.code in {'TO_ANALYSIS', 'ACTIONS_ASSIGNED'} or act.to_analysis_by_id == user.id
    return False


def can_send_to_ko(user, act):
    role = get_user_role(user)
    return (
        act.status.code == 'CREATED_OTK'
        and role in {'otk', 'manager', 'admin'}
        and (act.created_by_id == user.id or role in {'manager', 'admin'})
    )


def can_add_ko_decision(user, act):
    return act.status.code == 'KO_REVIEW' and get_user_role(user) in {'ko', 'manager', 'admin'}


def can_add_to_analysis(user, act):
    return act.status.code == 'TO_ANALYSIS' and get_user_role(user) in {'to', 'manager', 'admin'}


def get_role_context_text(user):
    role = get_user_role(user)
    if role == 'otk':
        return 'Показаны акты, созданные вами или ожидающие уточнения ОТК.'
    if role == 'ko':
        return 'Показаны акты на этапе решения КО.'
    if role == 'to':
        return 'Показаны акты на этапе анализа ТО и мероприятий.'
    if role in {'manager', 'admin'}:
        return 'Показаны все акты.'
    return 'Для пользователя без роли список актов ограничен.'
