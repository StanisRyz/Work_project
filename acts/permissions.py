from django.contrib.auth import get_user_model
from django.db.models import F, Q

from accounts.models import UserProfile
from accounts.roles import get_user_roles, has_any_role, has_role, role_holders_q

from . import workshops
from .models import Act, ActDefect


def get_user_profile(user):
    if not getattr(user, 'is_authenticated', False):
        return None
    try:
        profile = user.userprofile
    except (AttributeError, UserProfile.DoesNotExist):
        return None
    # An inactive profile grants no application role; superusers are handled separately.
    if profile.pk is None or not profile.is_active:
        return None
    return profile


def get_user_role(user):
    """The profile's own role — for display. Rights ask `get_user_roles()`."""
    profile = get_user_profile(user)
    return profile.role if profile else ''


# Every `is_*()` below asks the roles a user holds *today*: the profile's own
# plus any lent by a `RoleSubstitution` («замещение»). A technologist covering
# a designer is `is_to()` and `is_ko()` at once, so every rule written as
# «if is_otk … if is_ko …» must treat the roles as a union, never as a choice.


def is_otk(user):
    return has_role(user, UserProfile.Role.OTK)


# КО per workshop. Each decides the defects of its own цех; the retired general
# `KO` still decides every defect, as it always did, until an administrator
# moves its holders to a workshop role. `TR_SHOP` is planned: «КО ТР» exists
# and will decide those defects the day the workshop is introduced.
WORKSHOP_KO_ROLES = {
    workshops.MP_SHOP: UserProfile.Role.KO_MP,
    workshops.PIR_SHOP: UserProfile.Role.KO_PIR,
    workshops.TR_SHOP: UserProfile.Role.KO_TR,
}
KO_ROLES = frozenset({UserProfile.Role.KO, *WORKSHOP_KO_ROLES.values()})


def is_ko(user):
    """Any КО — the general one or a workshop one."""
    return has_any_role(user, KO_ROLES)


def ko_workshops(user):
    """The workshops whose defects `user` decides as КО.

    `None` means «every defect»: руководитель, администратор and the retired
    general КО. An empty set means none.
    """
    if has_full_act_access(user) or has_role(user, UserProfile.Role.KO):
        return None
    roles = get_user_roles(user)
    return frozenset(
        workshop for workshop, role in WORKSHOP_KO_ROLES.items() if role in roles
    )


def ko_roles_for_defect(defect):
    """The КО roles that decide this defect: its workshop's and the general one.

    A defect with no workshop (recorded before the choice was required) is not
    anybody's in particular, so every КО may decide it.
    """
    role = WORKSHOP_KO_ROLES.get(defect.workshop or '')
    if role is None:
        return KO_ROLES
    return frozenset({UserProfile.Role.KO, role})


def can_decide_defect(defect, user):
    """Whether `user` may enter the КО decision for this one defect."""
    allowed = ko_workshops(user)
    if allowed is None:
        return True
    if not defect.workshop:
        return bool(allowed) or is_ko(user)
    return defect.workshop in allowed


def decidable_defects(act, user):
    """This act's defects the user may decide as КО, in the act's order."""
    if _status_code(act) != 'KO_REVIEW' or not (is_ko(user) or has_full_act_access(user)):
        return []
    return [defect for defect in act.defects.all() if can_decide_defect(defect, user)]


def is_decided_this_round(defect, act):
    """A decision counts only if it was made in the act's current КО round."""
    return bool(
        act.ko_round
        and defect.ko_round == act.ko_round
        and defect.ko_decision
    )


def pending_ko_roles(act):
    """The КО roles still owing a decision on this act in the current round."""
    roles = set()
    defects = list(act.defects.all())
    if not defects:
        return KO_ROLES
    for defect in defects:
        if not is_decided_this_round(defect, act):
            roles |= ko_roles_for_defect(defect)
    return frozenset(roles)


def ko_roles_for_act(act):
    """Every КО role that decides some defect of this act."""
    defects = list(act.defects.all())
    if not defects:
        return KO_ROLES
    roles = set()
    for defect in defects:
        roles |= ko_roles_for_defect(defect)
    return frozenset(roles)


def is_to(user):
    return has_role(user, UserProfile.Role.TO)


def is_smk(user):
    """The Отдел СМК role — read exactly like every other role check.

    It grants nothing on acts or protocols; `smk.permissions` is where it
    means something. It lives here so no module invents a second way of
    asking what role a user has.
    """
    return has_role(user, UserProfile.Role.SMK)


def is_manager(user):
    return has_role(user, UserProfile.Role.MANAGER)


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


# --------------------------------------------------------------------------
# Who owns a `CREATED_OTK` act
#
# Normally its author, alone: they typed it and they are the one who sends it
# to КО. But an act returned from КО lands back in `CREATED_OTK`, and by then
# the author may have left, been deactivated or moved off ОТК — and a
# creator-only rule would strand the act with nobody able to edit or forward
# it. The fallback is deliberately narrow: it opens *only* while the creator
# is no longer an eligible active ОТК employee, and it never widens access to
# an act whose author is still there.
#
# `acts/services._move_act_workflow_task()` routes the `OTK_REWORK` queue entry
# by the same rule, so the person who gets the task is a person who may act on
# it.
# --------------------------------------------------------------------------


def creator_is_eligible_otk(act):
    """Whether this act's author may still work on it as ОТК.

    An active account *and* an active profile *and* the ОТК role — the same
    three conditions every other role check applies. A missing profile is read
    through `getattr`, because the row is deletable on its own in Admin.
    """
    creator = getattr(act, 'created_by', None)
    if creator is None or not creator.is_active:
        return False
    # ОТК held today, own or lent: an author who created the act while
    # substituting stops being «the author who is still there» when the
    # substitution ends, and the act is then open to ОТК at large.
    return UserProfile.Role.OTK in get_user_roles(creator)


def _eligible_otk_creator_filter():
    """The same rule as a `Q`, for the registry queryset."""
    return Q(
        created_by__is_active=True,
        created_by__userprofile__is_active=True,
    ) & role_holders_q(UserProfile.Role.OTK, prefix='created_by__')


def can_work_on_created_otk_act(act, user):
    """Whether `user` may edit or forward this `CREATED_OTK` act as ОТК.

    The author while the author is still eligible; any active ОТК employee once
    they are not. Managers and administrators are answered by
    `has_full_act_access()` in the callers, not here.
    """
    if not is_otk(user):
        return False
    if act.created_by_id == user.id:
        return True
    return not creator_is_eligible_otk(act)


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
    return bool(getattr(user, 'is_authenticated', False))


def can_contribute_to_act(act, user):
    """Return whether the act belongs to the user's current working scope."""
    if _status_code(act) == 'ARCHIVED':
        return False
    if has_full_act_access(user):
        return True
    status = _status_code(act)
    # A union, not a choice: a user may hold several roles at once through a
    # substitution, and each one opens its own stage.
    if is_otk(user):
        # `OTK_REVIEW` is the department's queue, not the author's: any active
        # ОТК employee reviews, returns and approves it. `CREATED_OTK` stays
        # the creator's own act — unless the creator is no longer an eligible
        # ОТК employee, which would otherwise leave a returned act stranded.
        if status == 'OTK_REVIEW':
            return True
        if status == 'CREATED_OTK' and can_work_on_created_otk_act(act, user):
            return True
    if is_ko(user) and status == 'KO_REVIEW' and _has_ko_share(act, user):
        return True
    if is_to(user) and (
        status == 'TO_ANALYSIS'
        or (status == 'ACTIONS_ASSIGNED' and act.to_analysis_by_id == user.id)
    ):
        return True
    return False


def can_send_to_ko(act, user):
    if _status_code(act) != 'CREATED_OTK':
        return False
    if has_full_act_access(user):
        return True
    return can_work_on_created_otk_act(act, user)


def can_edit_act(act, user):
    if _status_code(act) != 'CREATED_OTK':
        return False
    if has_full_act_access(user):
        return True
    return can_work_on_created_otk_act(act, user)


def _has_ko_share(act, user):
    """Whether some defect of this act is `user`'s to decide as КО.

    An act with no defects at all (the legacy act-level decision) is every
    КО's.
    """
    if not act.defects.exists():
        return True
    return any(can_decide_defect(defect, user) for defect in act.defects.all())


def can_apply_ko_decision(act, user):
    """КО of a workshop the act has defects in — or full access.

    Deciding is per defect: a workshop КО is offered only their own defects
    (`decidable_defects()`), and the act moves on to ТО once every defect has
    this round's decision.
    """
    if _status_code(act) != 'KO_REVIEW':
        return False
    if has_full_act_access(user):
        return True
    return is_ko(user) and _has_ko_share(act, user)


def can_return_to_otk(act, user):
    return can_apply_ko_decision(act, user)


def can_apply_to_analysis(act, user):
    return _status_code(act) == 'TO_ANALYSIS' and (is_to(user) or has_full_act_access(user))


def can_return_to_ko(act, user):
    return can_apply_to_analysis(act, user)


def can_review_otk(act, user):
    """Final ОТК review — «Вернуть в ТО» and «Утвердить».

    Any active ОТК employee, not only the act's author: the act is back with
    the department, and the person who created it may be away. Manager and
    administrator access is unchanged, and `can_return_to_to()` /
    `can_approve_act()` are this same rule so the backend and the UI cannot
    disagree.
    """
    if _status_code(act) != 'OTK_REVIEW':
        return False
    return has_full_act_access(user) or is_otk(user)


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
    return can_contribute_to_act(act, user)


def can_download_attachment(attachment, user):
    return can_view_act(attachment.act, user)


def can_delete_attachment(attachment, user):
    if not can_contribute_to_act(attachment.act, user):
        return False
    if has_full_act_access(user):
        return True
    return (
        getattr(user, 'is_authenticated', False)
        and attachment.uploaded_by_id is not None
        and attachment.uploaded_by_id == user.id
    )


def get_visible_acts_queryset(user):
    """Acts in the user's working queue; used by the ``my`` scope and mutations."""
    # No `operation`/`defect_type`: those legacy summary columns are not read
    # any more — defect data comes from the related `ActDefect` rows.
    queryset = Act.objects.select_related(
        'created_by',
        'priority',
        'status',
    )
    if has_full_act_access(user):
        return queryset
    # One condition per role held today, OR-ed: a substitute works both queues.
    condition = Q(pk__in=[])
    if is_otk(user):
        # Own acts still waiting to be sent to КО — plus any `CREATED_OTK` act
        # whose author is no longer an eligible ОТК employee, which nobody
        # else could otherwise pick up — plus every act the route brought back
        # for the final review, a queue that belongs to the department.
        eligible_authors = Act.objects.filter(_eligible_otk_creator_filter()).values('pk')
        condition |= (
            Q(status__code='CREATED_OTK')
            & (Q(created_by=user) | ~Q(pk__in=eligible_authors))
        ) | Q(status__code='OTK_REVIEW')
    if is_ko(user):
        allowed = ko_workshops(user)
        if allowed is None:
            condition |= Q(status__code='KO_REVIEW')
        else:
            # Acts with a defect of the user's workshop — or one recorded
            # without a workshop — still waiting for this round's decision.
            # Once their share is decided the act leaves their queue, though
            # they may still open it and correct their own decisions.
            waiting = (
                ActDefect.objects.filter(Q(workshop__in=allowed) | Q(workshop=''))
                .exclude(ko_round=F('act__ko_round'), ko_decision__gt='')
                .values('act_id')
            )
            condition |= Q(status__code='KO_REVIEW', pk__in=waiting)
    if is_to(user):
        condition |= Q(status__code='TO_ANALYSIS') | Q(
            status__code='ACTIONS_ASSIGNED', to_analysis_by=user,
        )
    return queryset.filter(condition)


def get_archived_acts_queryset(user):
    queryset = Act.objects.select_related('created_by', 'priority', 'status')
    if getattr(user, 'is_authenticated', False):
        return queryset.filter(status__code='ARCHIVED')
    return queryset.none()


def get_visible_acts_filter(user):
    """Return a `Q` matching every act an authenticated user may read."""
    if getattr(user, 'is_authenticated', False):
        return Q()
    return None


def get_all_visible_acts_queryset(user):
    """Every act an authenticated user may read — active and archived.

    The single readable queryset the real-time revision service builds
    its aggregates on. It carries no `select_related`, because its only job is
    to be counted and aggregated (or used as a subquery), never rendered — and
    it never materialises identifiers in Python, so a large registry costs the
    same number of queries as a small one.
    """
    condition = get_visible_acts_filter(user)
    if condition is None:
        return Act.objects.none()
    return Act.objects.filter(condition)


def get_full_act_access_users_queryset():
    """Active users with full workflow access to acts, resolved by the database.

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
            | Q(userprofile__role=UserProfile.Role.ADMIN)
            # A lent «Руководитель» is full access too, exactly as
            # `is_manager()` answers it; «Администратор» is never lent.
            | role_holders_q(UserProfile.Role.MANAGER)
        )
        .distinct()
    )


def _status_code(act):
    return getattr(getattr(act, 'status', None), 'code', '')
