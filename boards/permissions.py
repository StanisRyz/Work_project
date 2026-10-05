"""Who may create, manage, read and work on a board — the only place it is said.

Roles are read through `accounts.roles`, so a role lent by a substitution in
force today counts exactly like the profile's own, and an inactive account or
profile holds none. `Board.department` is organisational metadata and is never
asked here.

Nothing here decides who may *complete* a card. A card's work is a
`tasks.Task`, and finishing it is `tasks.permissions.can_complete_task()`,
unchanged: an assignee of the task, or an administrator.
"""

from accounts.models import UserProfile
from accounts.roles import has_any_role
from acts.permissions import is_act_admin


# Отдел продаж and ПДО keep boards; руководитель and администратор may start one
# for them. Every other role reads boards and works on the ones it is a member of.
BOARD_CREATOR_ROLES = frozenset({
    UserProfile.Role.PDO,
    UserProfile.Role.OPR,
    UserProfile.Role.MANAGER,
    UserProfile.Role.ADMIN,
})


def _is_authenticated(user):
    return bool(getattr(user, 'is_authenticated', False))


def is_active_employee(user):
    """An active account with an active profile — who may be on a board at all.

    The same condition every role-routed queryset in the project states
    (`is_active=True, userprofile__is_active=True`): a deactivated person is
    neither added to a board nor given its work.
    """
    if not getattr(user, 'is_active', False):
        return False
    profile = getattr(user, 'userprofile', None)
    return profile is not None and profile.pk is not None and profile.is_active


def can_view_board(user, board):
    """Every signed-in employee: a board's work is `tasks.Task`, which all read."""
    return _is_authenticated(user)


def can_create_board(user):
    """ПДО, Отдел продаж, руководитель, администратор — or a genuine superuser."""
    if not _is_authenticated(user):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return has_any_role(user, BOARD_CREATOR_ROLES)


def can_manage_board(user, board):
    """The owner, or an administrator: membership is theirs to change."""
    if not _is_authenticated(user):
        return False
    return user.pk == board.owner_id or is_act_admin(user)


def can_work_on_board(user, board):
    """An active member puts cards on the board, edits and moves them.

    An administrator may too, as everywhere else in the project. Reading the
    board grants none of it.
    """
    if not _is_authenticated(user):
        return False
    if is_act_admin(user):
        return True
    return is_active_employee(user) and board.members.filter(user=user).exists()
