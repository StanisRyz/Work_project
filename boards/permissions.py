"""Who may create, manage, read and work on a board — the only place it is said.

Roles are read through `accounts.roles`, so a role lent by a substitution in
force today counts exactly like the profile's own, and an inactive account or
profile holds none. `Board.department` is organisational metadata and is never
asked here.

Nothing here decides who may *complete* a card. A card's work is a
`tasks.Task`, and finishing it is `tasks.permissions.can_complete_task()`,
unchanged: an assignee of the task, or an administrator.

Every right below starts with board access (`can_use_boards()`): for now only
«Администратор» and a genuine superuser use boards at all. Without access the
answer is False, the owner's and a member's included — the boards and their
memberships stay as they are and simply grant nothing until access widens.

An archived board (`Board.Status.ARCHIVED`) is read-only for everybody:
nobody works on it, nobody manages its members or cancels its cards, and the
only right left on it is «Вернуть из архива» (`can_restore_board()`).
"""

from django.db.models import Q

from accounts.models import UserProfile
from accounts.roles import has_any_role, role_holders_q
from acts.permissions import is_act_admin


# Who uses boards at all. A temporary admission for the pilot, not a model of
# rights: boards are opened to more people by adding their roles here, and
# every rule below — owners, members, creators, исполнители — then works for
# them unchanged. Read at call time (`can_use_boards()`, `board_access_q()`),
# never copied at import, so a test may widen it.
BOARD_ACCESS_ROLES = frozenset({UserProfile.Role.ADMIN})

# Inside that admission: Отдел продаж and ПДО keep boards; руководитель and администратор may start one
# for them. Every other role reads boards and works on the ones it is a member of.
BOARD_CREATOR_ROLES = frozenset({
    UserProfile.Role.PDO,
    UserProfile.Role.OPR,
    UserProfile.Role.MANAGER,
    UserProfile.Role.ADMIN,
})


def _is_authenticated(user):
    return bool(getattr(user, 'is_authenticated', False))


def can_use_boards(user):
    """Board access: a genuine superuser, or a holder of a `BOARD_ACCESS_ROLES` role.

    Roles are read through `accounts.roles`, so an inactive account or profile
    holds none and a lent role would count like the profile's own. Asked first
    by every right below, by the menu, the dashboard card and the task registry
    (`tasks.permissions`), so a person without it never sees a board or its work.
    """
    if not _is_authenticated(user):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return has_any_role(user, BOARD_ACCESS_ROLES)


def board_access_q(prefix=''):
    """`can_use_boards()` as a filter on users; `prefix` walks a relation first.

    Only the access part: the caller adds `active_employee_q()` for the active
    account and profile. The substitution join can repeat a user, so a
    queryset built on it needs `.distinct()`.
    """
    condition = Q(**{f'{prefix}is_superuser': True})
    for role in BOARD_ACCESS_ROLES:
        condition |= role_holders_q(role, prefix=prefix)
    return condition


def active_employee_q(prefix=''):
    """«An active employee» as a filter: an active account with an active profile.

    `prefix` walks a relation first — `prefix='user__'` asks it about a
    `BoardMember`. The one filter-side statement of the rule
    `is_active_employee()` states for one user; the services filter with it
    and never restate it.
    """
    return Q(**{f'{prefix}is_active': True, f'{prefix}userprofile__is_active': True})


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
    """Every employee with board access (`can_use_boards()`)."""
    return can_use_boards(user)


def can_create_board(user):
    """ПДО, Отдел продаж, руководитель, администратор — or a genuine superuser.

    Within board access: today that leaves the administrator and the superuser.
    """
    if not can_use_boards(user):
        return False
    if getattr(user, 'is_superuser', False):
        return True
    return has_any_role(user, BOARD_CREATOR_ROLES)


def _keeps_board(user, board):
    """The owner while still an active employee, or an administrator — with access."""
    if not can_use_boards(user):
        return False
    if is_act_admin(user):
        return True
    return user.pk == board.owner_id and is_active_employee(user)


def can_manage_board(user, board):
    """Members and the shelf of a live board: its owner (active) or an administrator.

    A deactivated profile grants nothing, the owner's included. On an archived
    board management is reduced to `can_restore_board()`.
    """
    return _keeps_board(user, board) and not board.is_archived


def can_restore_board(user, board):
    """«Вернуть из архива» — the same people, and only for an archived board."""
    return _keeps_board(user, board) and board.is_archived


def can_cancel_card(user, card):
    """«Отменить карточку»: its author, the board's owner or an administrator.

    The author and the owner only while active employees. Never merely an
    исполнитель: the исполнитель *completes* the work, and withdrawing it is
    the decision of whoever put it on the board. Not on an archived board.
    Whether the task is still open is the service's question.
    """
    board = card.board
    if not can_use_boards(user) or board.is_archived:
        return False
    if is_act_admin(user):
        return True
    if not is_active_employee(user):
        return False
    return user.pk in (card.created_by_id, board.owner_id)


def can_comment_card(user, card):
    """Writing in a card's «Обсуждение»: whoever may work on its board.

    An active member or an administrator, never on an archived board — and
    whatever the state of the card's task: a completed or cancelled card is
    still discussed. Reading the discussion is reading the board.
    """
    return can_work_on_board(user, card.board)


def can_work_on_board(user, board):
    """An active member puts cards on the board, edits and moves them.

    An administrator may too, as everywhere else in the project. Reading the
    board grants none of it, and nobody works on an archived board.
    """
    if not can_use_boards(user) or board.is_archived:
        return False
    if is_act_admin(user):
        return True
    return is_active_employee(user) and board.members.filter(user=user).exists()
