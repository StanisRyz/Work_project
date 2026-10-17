"""Who reads «Утилизация МП» and who may dispose of prohibited МП.

Reading is every signed-in employee, as acts are. Disposing is the мастер of
«Цех МП» — «Мастер МП и РЛ», or the retired «Мастер производства», who still
stands for every цех — and the administrative fallback `is_act_admin()`. Roles
are read through `accounts.roles`, so a lent role counts.
"""

from accounts.models import UserProfile
from accounts.roles import has_any_role
from acts.permissions import is_act_admin


DISPOSAL_ROLES = frozenset({UserProfile.Role.MAS_MP_RL, UserProfile.Role.MAS})


def can_view_disposal(user):
    return bool(getattr(user, 'is_authenticated', False))


def can_dispose(user):
    if not getattr(user, 'is_authenticated', False):
        return False
    return is_act_admin(user) or has_any_role(user, DISPOSAL_ROLES)
