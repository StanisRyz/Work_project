"""Shared fixtures for the board tests."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, UserProfile

from ..models import BoardColumn, SubBoard
from ..services import create_board, create_card


# The default columns of a sub-board by the names the old fixed stages had —
# so a test written against «Сделать / В работе / На проверке / Готово»
# still says what it means.
STAGE_INDEX = {'TODO': 0, 'IN_PROGRESS': 1, 'REVIEW': 2}


def main_sub_board(board):
    """The board's first tab — «Основная» for a board `create_board()` made."""
    return SubBoard.objects.filter(board=board).order_by('position', 'pk').first()


def column_of(board, stage='TODO', sub_board=None):
    """The working column standing where the old `stage` stood (by place)."""
    sub_board = sub_board or main_sub_board(board)
    working = list(
        BoardColumn.objects.filter(sub_board=sub_board, is_done=False).order_by('position', 'pk')
    )
    return working[STAGE_INDEX[stage]]


def done_column_of(board, sub_board=None):
    return BoardColumn.objects.get(sub_board=sub_board or main_sub_board(board), is_done=True)


def stage_of(card):
    """The old stage name of the default column a card stands in."""
    card.refresh_from_db()
    for stage in STAGE_INDEX:
        if column_of(card.board, stage, card.sub_board).pk == card.column_id:
            return stage
    return None


def expected_counts(board, sub_board=None, *, TODO=0, IN_PROGRESS=0, REVIEW=0, DONE=0):
    """`column_counts()` as it should read, keyed by column id."""
    return {
        str(column_of(board, 'TODO', sub_board).pk): TODO,
        str(column_of(board, 'IN_PROGRESS', sub_board).pk): IN_PROGRESS,
        str(column_of(board, 'REVIEW', sub_board).pk): REVIEW,
        str(done_column_of(board, sub_board).pk): DONE,
    }


def board_url(board, sub_board=None):
    """The page of a board's sub-board (its first one by default)."""
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:sub_board', args=[board.pk, sub_board.pk])


def fragment_url(board, sub_board=None):
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:fragment', args=[board.pk, sub_board.pk])


def card_create_url(board, sub_board=None):
    sub_board = sub_board or main_sub_board(board)
    return reverse('boards:card_create', args=[board.pk, sub_board.pk])


def new_card(board, actor, title='Карточка', *, assignees, stage='TODO', sub_board=None,
             column=None, **extra):
    """`create_card()` on a board's sub-board, in `column` — or in the column
    the old `stage` named."""
    sub_board = sub_board or (column.sub_board if column is not None else main_sub_board(board))
    return create_card(
        sub_board,
        actor=actor,
        title=title,
        due_date=extra.pop('due_date', due()),
        assignee_ids=[getattr(user, 'pk', user) for user in assignees],
        column=column if column is not None else column_of(board, stage, sub_board),
        **extra,
    )


def department():
    # Seeded by `accounts.0003`; reused rather than duplicated.
    return Department.objects.get_or_create(code='PDO', defaults={'name': 'ПДО'})[0]


def make_user(username, role=UserProfile.Role.OTK, *, superuser=False):
    if superuser:
        user = User.objects.create_superuser(username=username, password='demo12345')
    else:
        user = User.objects.create_user(username=username, password='demo12345')
    profile = user.userprofile
    profile.role = role
    profile.department = department()
    profile.save()
    return user


def due(days=5):
    return timezone.localdate() + timedelta(days=days)


# Every role there is: board access as it will be once widened.
ALL_ROLES = frozenset(UserProfile.Role.values)


class WidenedBoardAccess:
    """Full board access open to every role for the whole test class.

    Full access — every board, creating one — is the administrator's only for
    now (`boards.permissions.BOARD_ACCESS_ROLES`); the board tests keep ПДО as
    a creator and owner and read boards as an outsider, so they run with full
    access widened — which is also what keeps the behaviour after widening
    covered. Patched before `setUpTestData()`, which already creates boards.
    `boards/tests/test_access.py` runs with the real constant: full access and
    reading by membership.
    """

    @classmethod
    def setUpClass(cls):
        patcher = mock.patch('boards.permissions.BOARD_ACCESS_ROLES', ALL_ROLES)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        super().setUpClass()


class BoardFixtureMixin(WidenedBoardAccess):
    """An ПДО-owned board with two ordinary members and one outsider."""

    @classmethod
    def setUpTestData(cls):
        cls.department = department()
        cls.owner = make_user('pdo_owner', UserProfile.Role.PDO)
        cls.member = make_user('member_one', UserProfile.Role.OTK)
        cls.colleague = make_user('member_two', UserProfile.Role.TO)
        cls.outsider = make_user('outsider', UserProfile.Role.OTK)
        cls.admin = make_user('admin_user', UserProfile.Role.ADMIN)
        cls.board = create_board(
            name='Планирование',
            department=cls.department,
            owner=cls.owner,
            actor=cls.owner,
            member_ids=[cls.member.pk, cls.colleague.pk],
        )
        cls.main = main_sub_board(cls.board)

    def card(self, title='Согласовать график', *, assignees=None, stage='TODO', actor=None, **extra):
        return new_card(
            self.board, actor or self.member, title,
            assignees=assignees or [self.member], stage=stage, **extra,
        )

    def column(self, stage='TODO'):
        return column_of(self.board, stage)

    def page_url(self):
        return board_url(self.board)
