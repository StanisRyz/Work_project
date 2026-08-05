from django.contrib.auth import get_user_model
from django.db.models import Q

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
    """Allow the destructive local reset only where it is enabled at all.

    Two independent gates, both required. `ENABLE_DEMO_RESET` decides whether
    the feature exists in this deployment — production forces it off and the
    URL is not even registered there — and the administrator role decides who
    may use it where it does exist.

    The old rule keyed on the literal username `admin_user`, which made a
    production safeguard depend on a demo account's name: renaming or seeding
    that account anywhere would have re-enabled a destructive action. The flag
    is the safeguard now.
    """
    from django.conf import settings

    return bool(getattr(settings, 'ENABLE_DEMO_RESET', False)) and is_act_admin(user)


def can_view_act(act, user):
    if has_full_act_access(user):
        return True
    if is_otk(user):
        return act.created_by_id == user.id and _status_code(act) in {'CREATED_OTK', 'OTK_REVIEW', 'ARCHIVED'}
    if is_ko(user):
        return _status_code(act) == 'KO_REVIEW' or (
            _status_code(act) == 'ARCHIVED' and act.ko_decision_by_id == user.id
        )
    if is_to(user):
        return _status_code(act) == 'TO_ANALYSIS' or (
            _status_code(act) in {'ACTIONS_ASSIGNED', 'ARCHIVED'} and act.to_analysis_by_id == user.id
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


def can_review_otk(act, user):
    if _status_code(act) != 'OTK_REVIEW':
        return False
    return has_full_act_access(user) or (is_otk(user) and act.created_by_id == user.id)


def can_return_to_to(act, user):
    return can_review_otk(act, user)


def can_approve_act(act, user):
    return can_review_otk(act, user)


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


def get_archived_acts_queryset(user):
    queryset = Act.objects.select_related('created_by', 'operation', 'defect_type', 'priority', 'status')
    if has_full_act_access(user):
        return queryset.filter(status__code='ARCHIVED')
    if is_otk(user):
        return queryset.filter(status__code='ARCHIVED', created_by=user)
    if is_ko(user):
        return queryset.filter(status__code='ARCHIVED', ko_decision_by=user)
    if is_to(user):
        return queryset.filter(status__code='ARCHIVED', to_analysis_by=user)
    return queryset.none()


def get_visible_acts_filter(user):
    """Return a `Q` matching exactly the acts `can_view_act` would allow.

    Deliberately mirrors `can_view_act` clause for clause — active *and*
    archived — so the two can never drift apart. Returns `None` when the user
    may see nothing at all, which callers turn into an empty queryset; an empty
    `Q()` would mean "everything" and is reserved for full access.
    """
    if has_full_act_access(user):
        return Q()
    if is_otk(user):
        return Q(created_by=user, status__code__in=['CREATED_OTK', 'OTK_REVIEW', 'ARCHIVED'])
    if is_ko(user):
        return Q(status__code='KO_REVIEW') | Q(status__code='ARCHIVED', ko_decision_by=user)
    if is_to(user):
        return (
            Q(status__code='TO_ANALYSIS')
            | Q(status__code='ACTIONS_ASSIGNED', to_analysis_by=user)
            | Q(status__code='ARCHIVED', to_analysis_by=user)
        )
    return None


def get_all_visible_acts_queryset(user):
    """Every act this user may see — active and archived — as one queryset.

    The single permission-aware queryset the real-time revision service builds
    its aggregates on. It carries no `select_related`, because its only job is
    to be counted and aggregated (or used as a subquery), never rendered — and
    it never materialises identifiers in Python, so a user who can see ten
    thousand acts costs the same number of queries as one who can see none.
    """
    condition = get_visible_acts_filter(user)
    if condition is None:
        return Act.objects.none()
    return Act.objects.filter(condition)


def get_full_act_access_users_queryset():
    """Active users who may already see any act, resolved by the database.

    `has_full_act_access` in Python would need every user loaded; this is the
    same rule (administrator role, superuser fallback, or manager) expressed as
    a filter, with the inactive-user and inactive-profile exclusions that
    `notifications.services.create_notifications` also applies.
    """
    return (
        get_user_model()
        .objects.filter(is_active=True, userprofile__is_active=True)
        .filter(
            Q(is_superuser=True)
            | Q(userprofile__role__in=[UserProfile.Role.ADMIN, UserProfile.Role.MANAGER])
        )
    )


def _status_code(act):
    return getattr(getattr(act, 'status', None), 'code', '')
