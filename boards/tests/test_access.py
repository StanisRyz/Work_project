"""Board access — for now administrators and genuine superusers only.

Everything here runs with the real `BOARD_ACCESS_ROLES`. Boards, members and
cards of people without access are created under a widened admission
(`widened()`), the way rows written before the admission narrowed would exist,
and then asked about with the real one.
"""

import io
import zipfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import AnonymousUser, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import RoleSubstitution, UserProfile
from notifications.models import Notification
from realtime.recipients import board_targets
from realtime.sync import REVISION_BOARDS, build_sync_state
from tasks.models import Task
from tasks.services import complete_task

from ..forms import AddMembersForm, BoardForm, CardForm
from ..models import BoardCard, BoardMember
from ..permissions import (
    BOARD_ACCESS_ROLES,
    can_cancel_card,
    can_comment_card,
    can_create_board,
    can_manage_board,
    can_restore_board,
    can_use_boards,
    can_view_board,
    can_work_on_board,
)
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    cancel_card,
    create_board,
    create_card,
    move_card,
    post_card_comment,
)
from .helpers import ALL_ROLES, department, due, make_user


def widened():
    """Board access as it will be once widened — to write rows of the past."""
    return mock.patch('boards.permissions.BOARD_ACCESS_ROLES', ALL_ROLES)


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def fresh(user):
    """The same account without the roles cached on the object."""
    return User.objects.get(pk=user.pk)


class AccessFixture:
    """A ПДО board with an ОТК member — and the administrators beside it."""

    @classmethod
    def setUpTestData(cls):
        cls.department = department()
        cls.admin = make_user('access_admin', UserProfile.Role.ADMIN)
        cls.admin_two = make_user('access_admin_two', UserProfile.Role.ADMIN)
        cls.superuser = make_user('access_root', UserProfile.Role.OTK, superuser=True)
        cls.pdo = make_user('access_pdo', UserProfile.Role.PDO)
        cls.otk = make_user('access_otk', UserProfile.Role.OTK)
        with widened():
            cls.board = create_board(
                name='Старая доска ПДО', department=cls.department, owner=cls.pdo,
                actor=cls.pdo, member_ids=[cls.otk.pk, cls.admin.pk],
            )
            cls.card = create_card(
                cls.board, actor=cls.pdo, title='Карточка ПДО', due_date=due(),
                assignee_ids=[cls.pdo.pk, cls.otk.pk], stage='TODO',
            )
            cls.done = create_card(
                cls.board, actor=cls.pdo, title='Сделанная ПДО', due_date=due(),
                assignee_ids=[cls.pdo.pk], stage='TODO',
            )
            complete_task(task_of(cls.done), cls.pdo, 'Готово')

    def setUp(self):
        # The roles lent today are cached on a user object; every test asks
        # about a fresh one, as a request would.
        for name in ('admin', 'admin_two', 'superuser', 'pdo', 'otk'):
            setattr(self, name, fresh(getattr(type(self), name)))


class CanUseBoardsTests(TestCase):
    def test_the_admission_is_the_administrator(self):
        self.assertEqual(BOARD_ACCESS_ROLES, frozenset({UserProfile.Role.ADMIN}))

    def test_administrator_and_superuser(self):
        self.assertTrue(can_use_boards(make_user('cu_admin', UserProfile.Role.ADMIN)))
        self.assertTrue(can_use_boards(make_user('cu_root', UserProfile.Role.OTK, superuser=True)))

    def test_nobody_else(self):
        for role in (
            UserProfile.Role.PDO, UserProfile.Role.OPR, UserProfile.Role.MANAGER,
            UserProfile.Role.OTK, UserProfile.Role.TO, UserProfile.Role.SMK,
        ):
            with self.subTest(role=role):
                self.assertFalse(can_use_boards(make_user(f'cu_{role}', role)))
        self.assertFalse(can_use_boards(AnonymousUser()))

    def test_no_profile(self):
        user = make_user('cu_no_profile', UserProfile.Role.ADMIN)
        UserProfile.objects.filter(user=user).delete()
        self.assertFalse(can_use_boards(User.objects.get(pk=user.pk)))

    def test_inactive_administrator(self):
        account = make_user('cu_inactive_account', UserProfile.Role.ADMIN)
        account.is_active = False
        account.save()
        self.assertFalse(can_use_boards(fresh(account)))
        profile_off = make_user('cu_inactive_profile', UserProfile.Role.ADMIN)
        UserProfile.objects.filter(user=profile_off).update(is_active=False)
        self.assertFalse(can_use_boards(fresh(profile_off)))

    def test_a_lent_role_gives_no_access(self):
        user = make_user('cu_to_covering_pdo', UserProfile.Role.TO)
        today = timezone.localdate()
        RoleSubstitution.objects.create(
            user=user, role=UserProfile.Role.PDO,
            date_from=today - timedelta(days=1), date_to=today + timedelta(days=1),
        )
        self.assertFalse(can_use_boards(fresh(user)))

    def test_widening_is_one_constant(self):
        pdo = make_user('cu_widened_pdo', UserProfile.Role.PDO)
        with widened():
            self.assertTrue(can_use_boards(fresh(pdo)))
        self.assertFalse(can_use_boards(fresh(pdo)))


class RightsWithoutAccessTests(AccessFixture, TestCase):
    def test_owner_and_member_hold_nothing(self):
        for user in (self.pdo, self.otk):
            with self.subTest(user=user.username):
                self.assertFalse(can_view_board(user, self.board))
                self.assertFalse(can_create_board(user))
                self.assertFalse(can_manage_board(user, self.board))
                self.assertFalse(can_work_on_board(user, self.board))
                self.assertFalse(can_comment_card(user, self.card))
                self.assertFalse(can_cancel_card(user, self.card))

    def test_restore_too(self):
        with widened():
            board = create_board(
                name='Архивная', department=self.department, owner=self.pdo, actor=self.pdo,
            )
            archive_board(board, actor=fresh(self.pdo))
        board.refresh_from_db()
        self.assertFalse(can_restore_board(fresh(self.pdo), board))
        self.assertTrue(can_restore_board(self.admin, board))

    def test_the_administrator_holds_them(self):
        self.assertTrue(can_view_board(self.admin, self.board))
        self.assertTrue(can_create_board(self.admin))
        self.assertTrue(can_manage_board(self.admin, self.board))
        self.assertTrue(can_work_on_board(self.admin, self.board))
        self.assertTrue(can_comment_card(self.admin, self.card))
        self.assertTrue(can_cancel_card(self.admin, self.card))

    def test_services_refuse_without_access(self):
        with self.assertRaises(BoardError):
            create_board(name='Новая', department=self.department, owner=self.pdo, actor=self.pdo)
        with self.assertRaises(BoardError):
            post_card_comment(self.card, actor=self.otk, text='Можно?')
        with self.assertRaises(BoardError):
            move_card(self.card, actor=self.pdo, stage='IN_PROGRESS')
        with self.assertRaises(BoardError):
            cancel_card(self.card, actor=self.pdo, reason='Не нужна')


class RouteTests(AccessFixture, TestCase):
    def routes(self):
        board, card = self.board.pk, self.card.pk
        return [
            ('boards:list', []),
            ('boards:create', []),
            ('boards:detail', [board]),
            ('boards:members', [board]),
            ('boards:archive', [board]),
            ('boards:restore', [board]),
            ('boards:members_add', [board]),
            ('boards:member_remove', [board, self.otk.pk]),
            ('boards:card_create', [board]),
            ('boards:card_update', [board, card]),
            ('boards:card_move', [board, card]),
            ('boards:card_complete', [board, card]),
            ('boards:card_cancel', [board, card]),
            ('boards:card_comment', [board, card]),
        ]

    def test_every_route_answers_403_before_the_method(self):
        self.client.force_login(self.pdo)
        for name, args in self.routes():
            url = reverse(name, args=args)
            with self.subTest(route=name):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {'text': 'x', 'stage': 'TODO'}).status_code, 403)
        self.assertEqual(BoardCard.objects.get(pk=self.card.pk).stage, 'TODO')
        self.assertEqual(BoardMember.objects.filter(board=self.board).count(), 3)

    def test_json_routes_answer_json_403(self):
        self.client.force_login(self.otk)
        response = self.client.get(reverse('boards:fragment', args=[self.board.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card.pk]),
            {'stage': 'IN_PROGRESS'}, HTTP_X_REQUESTED_WITH='fetch',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['ok'], False)
        self.assertEqual(BoardCard.objects.get(pk=self.card.pk).stage, 'TODO')

    def test_the_administrator_gets_the_pages(self):
        self.client.force_login(self.admin)
        for name, args in (
            ('boards:list', []), ('boards:create', []), ('boards:detail', [self.board.pk]),
            ('boards:members', [self.board.pk]), ('boards:fragment', [self.board.pk]),
        ):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 200)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card.pk]),
            {'stage': 'IN_PROGRESS'}, HTTP_X_REQUESTED_WITH='fetch',
        )
        self.assertEqual(response.status_code, 200)


class NavigationTests(AccessFixture, TestCase):
    def test_menu_and_dashboard(self):
        boards_url = reverse('boards:list')
        self.client.force_login(self.pdo)
        response = self.client.get(reverse('dashboard:home'))
        self.assertNotContains(response, f'href="{boards_url}"')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard:home'))
        # The sidebar link and the «Быстрый доступ» card.
        self.assertContains(response, f'href="{boards_url}"', count=2)


class TaskVisibilityTests(AccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.task = task_of(self.card)
        self.done_task = task_of(self.done)

    def registry(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('tasks:list'), params)

    def test_registry_tabs_hide_board_tasks_without_access(self):
        for tab in ('my', 'all', 'archive'):
            with self.subTest(tab=tab):
                content = self.registry(self.pdo, tab=tab).content.decode()
                self.assertNotIn('Карточка ПДО', content)
                self.assertNotIn('Сделанная ПДО', content)
        counts = self.registry(self.pdo).context['tab_counts']
        self.assertEqual(counts, {'my': 0, 'all': 0, 'archive': 0})
        self.assertContains(self.registry(self.admin, tab='all'), 'Карточка ПДО')
        self.assertContains(self.registry(self.admin, tab='archive'), 'Сделанная ПДО')

    def test_the_type_filter_offers_the_board_only_with_access(self):
        option = '<option value="BOARD"'
        self.assertNotContains(self.registry(self.pdo, tab='all'), option)
        self.assertContains(self.registry(self.admin, tab='all'), option)

    def test_excel_export(self):
        def sheet_text(user):
            response = self.registry(user, tab='all', export='xlsx')
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                return ''.join(
                    archive.read(name).decode('utf-8')
                    for name in archive.namelist() if name.endswith('.xml')
                )
        self.assertNotIn('Карточка ПДО', sheet_text(self.pdo))
        self.assertIn('Карточка ПДО', sheet_text(self.admin))

    def test_task_page_is_404_without_access(self):
        self.client.force_login(self.pdo)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[self.task.pk])).status_code, 404)
        self.client.force_login(self.admin)
        response = self.client.get(reverse('tasks:detail', args=[self.task.pk]))
        self.assertRedirects(
            response, f"{reverse('boards:detail', args=[self.board.pk])}?card={self.card.pk}",
            fetch_redirect_response=False,
        )

    def test_quick_search(self):
        self.client.force_login(self.pdo)
        response = self.client.get(reverse('dashboard:search'), {'q': 'Карточка ПДО'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, f'Задача №{self.task.pk}')
        self.client.force_login(self.admin)
        response = self.client.get(reverse('dashboard:search'), {'q': 'Карточка ПДО'})
        # A single hit opens the task, which opens the card.
        self.assertRedirects(
            response, reverse('tasks:detail', args=[self.task.pk]), fetch_redirect_response=False,
        )

    def test_dashboard_my_tasks_and_counts(self):
        self.client.force_login(self.pdo)
        response = self.client.get(reverse('dashboard:home'))
        self.assertNotContains(response, 'Карточка ПДО')
        from dashboard.summary import my_work_counts

        self.assertEqual(my_work_counts(self.pdo)['tasks'], 0)
        self.assertEqual(my_work_counts(self.otk)['tasks'], 0)

    def test_completion_route_is_404_without_access(self):
        self.client.force_login(self.pdo)
        response = self.client.post(
            reverse('tasks:complete', args=[self.task.pk]), {'execution_comment': 'Сделано'},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Task.objects.get(pk=self.task.pk).status.code, 'IN_PROGRESS')


class MembershipTests(AccessFixture, TestCase):
    def test_forms_offer_only_people_with_access(self):
        offered = set(BoardForm(owner=self.admin).fields['members'].queryset)
        self.assertIn(self.admin_two, offered)
        self.assertIn(self.superuser, offered)
        self.assertNotIn(self.pdo, offered)
        self.assertNotIn(self.otk, offered)
        candidates = set(AddMembersForm(board=self.board).fields['users'].queryset)
        self.assertIn(self.admin_two, candidates)
        self.assertNotIn(fresh(make_user('access_new_pdo', UserProfile.Role.PDO)), candidates)

    def test_the_card_form_offers_only_members_with_access(self):
        offered = set(CardForm(board=self.board).fields['assignees'].queryset)
        self.assertEqual(offered, {self.admin})

    def test_services_refuse_a_hand_sent_id(self):
        with self.assertRaises(BoardError):
            create_board(
                name='Новая', department=self.department, owner=self.admin, actor=self.admin,
                member_ids=[self.pdo.pk],
            )
        with self.assertRaises(BoardError):
            add_board_members(self.board, [self.pdo.pk, self.admin_two.pk], actor=self.admin)
        self.assertFalse(BoardMember.objects.filter(board=self.board, user=self.admin_two).exists())
        added = add_board_members(self.board, [self.admin_two.pk], actor=self.admin)
        self.assertEqual(added, [self.admin_two.pk])

    def test_a_member_row_without_access_carries_no_work(self):
        # ОТК is a member from before; the administrator cannot put a card on them.
        with self.assertRaises(BoardError):
            create_card(
                self.board, actor=self.admin, title='Нельзя', due_date=due(),
                assignee_ids=[self.otk.pk], stage='TODO',
            )
        card = create_card(
            self.board, actor=self.admin, title='Можно', due_date=due(),
            assignee_ids=[self.admin.pk], stage='TODO',
        )
        self.assertEqual([a.user for a in task_of(card).assignees.all()], [self.admin])

    def test_no_notice_reaches_anybody_without_access(self):
        board = create_board(
            name='Админская', department=self.department, owner=self.admin, actor=self.admin,
            member_ids=[self.admin_two.pk],
        )
        Notification.objects.all().delete()
        card = create_card(
            board, actor=self.admin, title='Задача админам', due_date=due(),
            assignee_ids=[self.admin_two.pk], stage='TODO',
        )
        post_card_comment(card, actor=self.admin, text='Вопрос')
        cancel_card(card, actor=self.admin, reason='Передумали')
        recipients = set(Notification.objects.values_list('recipient_id', flat=True))
        self.assertTrue(recipients)
        self.assertLessEqual(recipients, {self.admin.pk, self.admin_two.pk})


class RealtimeTests(AccessFixture, TestCase):
    def test_board_targets_are_the_people_with_access(self):
        inactive = make_user('access_admin_off', UserProfile.Role.ADMIN)
        inactive.is_active = False
        inactive.save()
        targets = {target.identifier for target in board_targets(self.board)}
        self.assertEqual(targets, {self.admin.pk, self.admin_two.pk, self.superuser.pk})

    def test_the_boards_revision_does_not_move_without_access(self):
        before_pdo = build_sync_state(self.pdo)['revisions'][REVISION_BOARDS]
        before_admin = build_sync_state(self.admin)['revisions'][REVISION_BOARDS]
        create_card(
            self.board, actor=self.admin, title='Новая', due_date=due(),
            assignee_ids=[self.admin.pk], stage='TODO',
        )
        self.assertEqual(build_sync_state(fresh(self.pdo))['revisions'][REVISION_BOARDS], before_pdo)
        self.assertNotEqual(
            build_sync_state(fresh(self.admin))['revisions'][REVISION_BOARDS], before_admin,
        )
