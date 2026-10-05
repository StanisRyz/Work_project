"""Who may create, manage and work on a board."""

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.utils import timezone

from accounts.models import RoleSubstitution, UserProfile

from ..permissions import (
    BOARD_CREATOR_ROLES,
    can_create_board,
    can_manage_board,
    can_view_board,
    can_work_on_board,
)
from .helpers import BoardFixtureMixin, make_user


class CreateBoardPermissionTests(TestCase):
    def test_every_creator_role_may_create(self):
        for role in BOARD_CREATOR_ROLES:
            with self.subTest(role=role):
                self.assertTrue(can_create_board(make_user(f'creator_{role}', role)))

    def test_other_roles_may_not(self):
        for role in (
            UserProfile.Role.OTK, UserProfile.Role.KO_MP, UserProfile.Role.TO,
            UserProfile.Role.MAS, UserProfile.Role.SMK, UserProfile.Role.OZK,
        ):
            with self.subTest(role=role):
                self.assertFalse(can_create_board(make_user(f'plain_{role}', role)))

    def test_lent_role_counts(self):
        user = make_user('to_covering_pdo', UserProfile.Role.TO)
        today = timezone.localdate()
        RoleSubstitution.objects.create(
            user=user, role=UserProfile.Role.PDO,
            date_from=today - timedelta(days=1), date_to=today + timedelta(days=1),
        )
        self.assertTrue(can_create_board(user))

    def test_expired_substitution_does_not(self):
        user = make_user('to_was_pdo', UserProfile.Role.TO)
        today = timezone.localdate()
        RoleSubstitution.objects.create(
            user=user, role=UserProfile.Role.PDO,
            date_from=today - timedelta(days=5), date_to=today - timedelta(days=1),
        )
        self.assertFalse(can_create_board(user))

    def test_inactive_profile_grants_nothing(self):
        user = make_user('inactive_pdo', UserProfile.Role.PDO)
        user.userprofile.is_active = False
        user.userprofile.save()
        self.assertFalse(can_create_board(user))

    def test_superuser_may_create(self):
        self.assertTrue(can_create_board(make_user('root', UserProfile.Role.OTK, superuser=True)))

    def test_anonymous_may_not(self):
        self.assertFalse(can_create_board(AnonymousUser()))


class BoardAccessTests(BoardFixtureMixin, TestCase):
    def test_everybody_signed_in_reads(self):
        self.assertTrue(can_view_board(self.outsider, self.board))
        self.assertFalse(can_view_board(AnonymousUser(), self.board))

    def test_owner_and_admin_manage(self):
        self.assertTrue(can_manage_board(self.owner, self.board))
        self.assertTrue(can_manage_board(self.admin, self.board))
        self.assertFalse(can_manage_board(self.member, self.board))

    def test_members_and_admin_work(self):
        self.assertTrue(can_work_on_board(self.member, self.board))
        self.assertTrue(can_work_on_board(self.owner, self.board))
        self.assertTrue(can_work_on_board(self.admin, self.board))
        self.assertFalse(can_work_on_board(self.outsider, self.board))

    def test_deactivated_member_does_not_work(self):
        self.member.userprofile.is_active = False
        self.member.userprofile.save()
        self.assertFalse(can_work_on_board(self.member, self.board))
