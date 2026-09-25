"""What roles a user holds today: the profile's own, plus substitutions in force.

The one answer every permission in the project asks. `acts.permissions`
wraps it in the `is_*()` helpers the other modules already import, and the
queryset side — «who holds КО right now», for routing tasks and notifications
— is `role_holders_q()`, the same rule written as a filter, so the Python and
the database answers cannot disagree.

A substitution (`RoleSubstitution`) only ever *adds* a role, for a period,
inclusive at both ends. An inactive account or an inactive profile holds no
role at all — its own or a lent one — exactly as before substitutions existed.
«Администратор» is never lent: the model refuses it, and `is_act_admin()`
keeps reading the profile.
"""

from django.db.models import Q
from django.utils import timezone

from .models import RoleSubstitution


# The lent roles of one user, remembered on the user object for the rest of the
# request: a page asks «is this user КО» dozens of times, and each would
# otherwise be a query. Keyed by the day, so a long-lived object never carries
# yesterday's answer into today.
_CACHE_ATTR = '_quality_substituted_roles'


def _active_profile(user):
    if not getattr(user, 'is_authenticated', False) or not getattr(user, 'is_active', True):
        return None
    profile = getattr(user, 'userprofile', None)
    if profile is None or profile.pk is None or not profile.is_active:
        return None
    return profile


def active_substitutions(user, day=None):
    """The substitutions in force for `user` on `day` (today by default)."""
    if _active_profile(user) is None:
        return RoleSubstitution.objects.none()
    day = day or timezone.localdate()
    return (
        RoleSubstitution.objects.filter(user=user, date_from__lte=day, date_to__gte=day)
        .select_related('substitutes_for')
        .order_by('date_to', 'pk')
    )


def _substituted_roles(user):
    day = timezone.localdate()
    cached = getattr(user, _CACHE_ATTR, None)
    if cached is not None and cached[0] == day:
        return cached[1]
    roles = frozenset(active_substitutions(user, day).values_list('role', flat=True))
    try:
        setattr(user, _CACHE_ATTR, (day, roles))
    except AttributeError:
        pass
    return roles


def forget_cached_roles(user):
    """Drop the per-object cache — after a substitution is saved in the same request."""
    if hasattr(user, _CACHE_ATTR):
        delattr(user, _CACHE_ATTR)


def get_user_roles(user):
    """Every role `user` holds today: the profile's own and the lent ones."""
    profile = _active_profile(user)
    if profile is None:
        return frozenset()
    return frozenset({profile.role}) | _substituted_roles(user)


def has_role(user, role):
    return role in get_user_roles(user)


def has_any_role(user, roles):
    return bool(get_user_roles(user) & frozenset(roles))


def substitution_for(user, role, day=None):
    """The substitution that gives `user` a role they do not hold themselves.

    `None` when the profile already carries the role, or when nothing lends
    it. What the act history quotes as «замещает …».
    """
    profile = _active_profile(user)
    if profile is None or profile.role == role:
        return None
    if day is None and role not in _substituted_roles(user):
        # The cached answer already says nothing lends it.
        return None
    return active_substitutions(user, day).filter(role=role).first()


def role_holders_q(role, *, prefix='', day=None):
    """`role_holders_q(role)` is `has_role(user, role)` as a filter on users.

    `prefix` walks a relation first — `prefix='created_by__'` asks it about an
    act's author. Only the role part is here; the caller adds the
    active-account and active-profile conditions it already states. A join
    through the substitutions can repeat a user, so a queryset built on it
    needs `.distinct()`.
    """
    day = day or timezone.localdate()
    return Q(**{f'{prefix}userprofile__role': role}) | Q(
        **{
            f'{prefix}role_substitutions__role': role,
            f'{prefix}role_substitutions__date_from__lte': day,
            f'{prefix}role_substitutions__date_to__gte': day,
        }
    )


def describe_roles(user):
    """The profile's role label followed by what is lent today, for the header.

    «ТО, замещает КО до 10.10» — or just «ТО» when nothing is lent.
    """
    profile = _active_profile(user)
    if profile is None:
        profile = getattr(user, 'userprofile', None)
        return profile.role_label if profile is not None else ''
    parts = [profile.role_label]
    for substitution in active_substitutions(user):
        if substitution.role == profile.role:
            continue
        parts.append(
            f'замещает {substitution.role_label} до {substitution.date_to:%d.%m}'
        )
    return ', '.join(parts)

