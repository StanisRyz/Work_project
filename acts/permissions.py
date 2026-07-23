from accounts.models import UserProfile

from .models import Act


def get_user_profile(user):
    if not getattr(user, 'is_authenticated', False):
        return None
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return None
    if profile.pk is None:
        return None
    return profile


def get_user_role(user):
    profile = get_user_profile(user)
    return profile.role if profile else ''


def is_otk(user):
    return get_user_role(user) == UserProfile.Role.OTK


def is_ko(user):
    return get_user_role(user) == UserProfile.Role.KO


def is_to(user):
    return get_user_role(user) == UserProfile.Role.TO


def is_manager(user):
    return get_user_role(user) == UserProfile.Role.MANAGER


def is_admin(user):
    return is_act_admin(user)


def is_act_admin(user):
    """Return whether a user has the explicit administrator act role or superuser fallback."""
    return bool(
        getattr(user, 'is_authenticated', False)
        and (
            getattr(user, 'is_superuser', False)
            or get_user_role(user) == UserProfile.Role.ADMIN
        )
    )


def has_full_act_access(user):
    """Return whether a user may see every act and use status-valid actions."""
    return is_act_admin(user) or is_manager(user)


def is_manager_or_admin(user):
    return has_full_act_access(user)


def can_create_act(user):
    return is_otk(user) or is_manager_or_admin(user)


def can_clear_all_acts(user):
    """Allow the destructive local reset only for the dedicated demo administrator."""
    return is_act_admin(user) and getattr(user, 'username', '') == 'admin_user'


def can_view_act(act, user):
    if has_full_act_access(user):
        return True
    if is_otk(user):
        return act.created_by_id == user.id and _status_code(act) in {'CREATED_OTK', 'OTK_REVIEW'}
    if is_ko(user):
        return _status_code(act) == 'KO_REVIEW'
    if is_to(user):
        return _status_code(act) == 'TO_ANALYSIS' or (
            _status_code(act) == 'ACTIONS_ASSIGNED' and act.to_analysis_by_id == user.id
        )
    return False


def can_send_to_ko(act, user):
    if _status_code(act) != 'CREATED_OTK':
        return False
    if has_full_act_access(user):
        return True
    return is_otk(user) and act.created_by_id == user.id


def can_edit_act(act, user):
    if _status_code(act) != 'CREATED_OTK':
        return False
    if has_full_act_access(user):
        return True
    return is_otk(user) and act.created_by_id == user.id


def can_apply_ko_decision(act, user):
    return _status_code(act) == 'KO_REVIEW' and (is_ko(user) or has_full_act_access(user))


def can_return_to_otk(act, user):
    return can_apply_ko_decision(act, user)


def can_apply_to_analysis(act, user):
    return _status_code(act) == 'TO_ANALYSIS' and (is_to(user) or has_full_act_access(user))


def can_return_to_ko(act, user):
    return can_apply_to_analysis(act, user)


def can_close_act(act, user):
    if _status_code(act) != 'ACTIONS_ASSIGNED':
        return False
    if has_full_act_access(user):
        return True
    return is_to(user) and act.to_analysis_by_id == user.id


def can_add_attachment(act, user):
    return can_view_act(act, user)


def can_download_attachment(attachment, user):
    return can_view_act(attachment.act, user)


def can_delete_attachment(attachment, user):
    if has_full_act_access(user):
        return True
    return (
        getattr(user, 'is_authenticated', False)
        and attachment.uploaded_by_id is not None
        and attachment.uploaded_by_id == user.id
    )


def get_visible_acts_queryset(user):
    queryset = Act.objects.select_related(
        'created_by',
        'operation',
        'defect_type',
        'priority',
        'status',
    )
    if has_full_act_access(user):
        return queryset
    if is_otk(user):
        return queryset.filter(created_by=user, status__code__in=['CREATED_OTK', 'OTK_REVIEW'])
    if is_ko(user):
        return queryset.filter(status__code='KO_REVIEW')
    if is_to(user):
        return queryset.filter(status__code='TO_ANALYSIS') | queryset.filter(
            status__code='ACTIONS_ASSIGNED',
            to_analysis_by=user,
        )
    return queryset.none()


def _status_code(act):
    return getattr(getattr(act, 'status', None), 'code', '')
