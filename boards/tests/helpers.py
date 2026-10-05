"""Shared fixtures for the board tests."""

from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.utils import timezone

from accounts.models import Department, UserProfile

from ..services import create_board, create_card


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
    """Board access open to every role for the whole test class.

    Boards are admitted to administrators only for now
    (`boards.permissions.BOARD_ACCESS_ROLES`); the board tests keep ПДО, ОТК
    and ТО as owners and members, so they run with the admission widened —
    which is also what keeps the behaviour after widening covered. Patched
    before `setUpTestData()`, which already creates boards.
    `boards/tests/test_access.py` runs with the real constant.
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

    def card(self, title='Согласовать график', *, assignees=None, stage='TODO', actor=None, **extra):
        return create_card(
            self.board,
            actor=actor or self.member,
            title=title,
            due_date=extra.pop('due_date', due()),
            assignee_ids=[user.pk for user in (assignees or [self.member])],
            stage=stage,
            **extra,
        )
