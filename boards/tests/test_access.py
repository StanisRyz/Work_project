"""Who reaches boards: full access, or membership.

Everything here runs with the real `BOARD_ACCESS_ROLES` ({«Администратор»}):
an administrator or a genuine superuser reads every board and creates them;
anybody else reads the boards they are a member of, works on their cards and
sees their tasks — and nothing of any other board.
"""

import io
import zipfile
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import Department, RoleSubstitution, UserProfile
from dashboard.summary import my_work_counts
from realtime.recipients import board_targets
from realtime.sync import REVISION_BOARDS, build_sync_state
from tasks.models import Task

from ..forms import AddMembersForm, BoardForm, CardForm
from ..models import Board, BoardMember
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
    has_full_board_access,
)
from ..services import (
    BoardError,
    add_board_members,
    archive_board,
    create_board,
    post_card_comment,
)
from .helpers import board_url, department, fragment_url, fresh_code, make_user, new_card, stage_of


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def fresh(user):
    """The same account without anything cached on the object, as a request sees it."""
    return User.objects.get(pk=user.pk)


def other_department():
    # Seeded by `accounts.0008`; reused rather than duplicated.
    return Department.objects.get_or_create(code='OPR', defaults={'name': 'Отдел продаж'})[0]


class AccessFixture:
    """An administrators' board with an ОТК member — and a board the ОТК member is not on."""

    @classmethod
    def setUpTestData(cls):
        cls.department = department()
        cls.admin = make_user('access_admin', UserProfile.Role.ADMIN)
        cls.admin_two = make_user('access_admin_two', UserProfile.Role.ADMIN)
        cls.superuser = make_user('access_root', UserProfile.Role.OTK, superuser=True)
        cls.otk = make_user('access_otk', UserProfile.Role.OTK)
        cls.pdo = make_user('access_pdo', UserProfile.Role.PDO)
        cls.loner = make_user('access_loner', UserProfile.Role.TO)
        cls.board = create_board(
            code=fresh_code(),
            name='Доска ОТК', owner=cls.admin, actor=cls.admin, member_ids=[cls.otk.pk],
        )
        cls.card = new_card(cls.board, cls.admin, 'Карточка ОТК', assignees=[cls.otk])
        cls.foreign = create_board(
            code=fresh_code(),
            name='Чужая доска', owner=cls.admin, actor=cls.admin, member_ids=[cls.pdo.pk],
        )
        cls.foreign_card = new_card(cls.foreign, cls.admin, 'Карточка ПДО', assignees=[cls.pdo])

    def setUp(self):
        for name in ('admin', 'admin_two', 'superuser', 'otk', 'pdo', 'loner'):
            setattr(self, name, fresh(getattr(type(self), name)))


class FullAccessTests(TestCase):
    def test_the_admission_is_the_administrator(self):
        self.assertEqual(BOARD_ACCESS_ROLES, frozenset({UserProfile.Role.ADMIN}))

    def test_administrator_and_superuser(self):
        self.assertTrue(has_full_board_access(make_user('fa_admin', UserProfile.Role.ADMIN)))
        self.assertTrue(has_full_board_access(make_user('fa_root', UserProfile.Role.OTK, superuser=True)))

    def test_nobody_else(self):
        for role in (
            UserProfile.Role.PDO, UserProfile.Role.OPR, UserProfile.Role.MANAGER,
            UserProfile.Role.OTK, UserProfile.Role.TO, UserProfile.Role.SMK,
        ):
            with self.subTest(role=role):
                self.assertFalse(has_full_board_access(make_user(f'fa_{role}', role)))
        self.assertFalse(has_full_board_access(AnonymousUser()))

    def test_inactive_administrator_and_lent_role(self):
        account = make_user('fa_inactive', UserProfile.Role.ADMIN)
        account.is_active = False
        account.save()
        self.assertFalse(has_full_board_access(fresh(account)))
        user = make_user('fa_to_covering_pdo', UserProfile.Role.TO)
        today = timezone.localdate()
        RoleSubstitution.objects.create(
            user=user, role=UserProfile.Role.PDO,
            date_from=today - timedelta(days=1), date_to=today + timedelta(days=1),
        )
        self.assertFalse(has_full_board_access(fresh(user)))


class CanUseBoardsTests(AccessFixture, TestCase):
    def test_full_access_and_members_use_boards(self):
        for user in (self.admin, self.superuser, self.otk, self.pdo):
            with self.subTest(user=user.username):
                self.assertTrue(can_use_boards(user))

    def test_nobody_on_no_board(self):
        self.assertFalse(can_use_boards(self.loner))
        self.assertFalse(can_use_boards(AnonymousUser()))

    def test_an_inactive_member_gets_nothing(self):
        UserProfile.objects.filter(user=self.otk).update(is_active=False)
        otk = fresh(self.otk)
        self.assertFalse(can_use_boards(otk))
        self.assertFalse(can_view_board(otk, self.board))
        self.assertFalse(can_work_on_board(otk, self.board))

    def test_one_query_per_request(self):
        # The profile, the lent roles, the membership — once per request.
        with self.assertNumQueries(3):
            self.assertTrue(can_use_boards(self.otk))
        with self.assertNumQueries(0):
            self.assertTrue(can_use_boards(self.otk))


class RightsTests(AccessFixture, TestCase):
    def test_a_member_reads_and_works_on_their_board(self):
        self.assertTrue(can_view_board(self.otk, self.board))
        self.assertTrue(can_work_on_board(self.otk, self.board))
        self.assertTrue(can_comment_card(self.otk, self.card))
        # Neither the owner nor an administrator, and full access creates boards.
        self.assertFalse(can_manage_board(self.otk, self.board))
        self.assertFalse(can_cancel_card(self.otk, self.card))
        self.assertFalse(can_create_board(self.otk))

    def test_nothing_on_a_board_they_are_not_on(self):
        self.assertFalse(can_view_board(self.otk, self.foreign))
        self.assertFalse(can_work_on_board(self.otk, self.foreign))
        self.assertFalse(can_comment_card(self.otk, self.foreign_card))
        self.assertFalse(can_cancel_card(self.otk, self.foreign_card))
        with self.assertRaises(BoardError):
            post_card_comment(self.foreign_card, actor=self.otk, text='Можно?')

    def test_an_archived_board_is_read_the_same_way(self):
        board = create_board(
            code=fresh_code(),
            name='Архивная', owner=self.admin, actor=self.admin, member_ids=[self.otk.pk],
        )
        archive_board(board, actor=self.admin)
        board.refresh_from_db()
        self.assertTrue(can_view_board(fresh(self.otk), board))
        self.assertFalse(can_work_on_board(fresh(self.otk), board))
        self.assertFalse(can_view_board(fresh(self.loner), board))
        self.assertTrue(can_restore_board(self.admin, board))
        self.assertFalse(can_restore_board(fresh(self.otk), board))

    def test_an_owner_without_full_access_still_keeps_the_board(self):
        board = Board.objects.create(name='Старая', code=fresh_code(), owner=self.otk)
        BoardMember.objects.create(board=board, user=self.otk)
        self.assertTrue(can_manage_board(self.otk, board))

    def test_the_administrator_sees_everything(self):
        for board in (self.board, self.foreign):
            with self.subTest(board=board.name):
                self.assertTrue(can_view_board(self.admin, board))
                self.assertTrue(can_manage_board(self.admin, board))
                self.assertTrue(can_work_on_board(self.admin, board))
        self.assertTrue(can_view_board(self.superuser, self.foreign))
        self.assertTrue(can_create_board(self.admin))


class RouteTests(AccessFixture, TestCase):
    def foreign_routes(self):
        board, card = self.foreign.pk, self.foreign_card.pk
        self.foreign_sub = self.foreign.sub_boards.get()
        self.foreign_column = self.foreign_sub.columns.order_by('position').first()
        return [
            ('boards:detail', [board]),
            ('boards:sub_board', [board, self.foreign_sub.pk]),
            ('boards:members', [board]),
            ('boards:archive', [board]),
            ('boards:restore', [board]),
            ('boards:members_add', [board]),
            ('boards:member_remove', [board, self.pdo.pk]),
            ('boards:card_create', [board, self.foreign_sub.pk]),
            ('boards:sub_board_create', [board, self.foreign_sub.pk]),
            ('boards:sub_board_rename', [board, self.foreign_sub.pk]),
            ('boards:sub_board_move', [board, self.foreign_sub.pk]),
            ('boards:sub_board_delete', [board, self.foreign_sub.pk]),
            ('boards:column_create', [board, self.foreign_sub.pk]),
            ('boards:column_rename', [board, self.foreign_sub.pk, self.foreign_column.pk]),
            ('boards:column_move', [board, self.foreign_sub.pk, self.foreign_column.pk]),
            ('boards:column_delete', [board, self.foreign_sub.pk, self.foreign_column.pk]),
            ('boards:card_update', [board, card]),
            ('boards:card_move', [board, card]),
            ('boards:card_complete', [board, card]),
            ('boards:card_cancel', [board, card]),
            ('boards:card_comment', [board, card]),
        ]

    def test_a_member_opens_their_board(self):
        self.client.force_login(self.otk)
        self.assertRedirects(
            self.client.get(reverse('boards:list')), reverse('boards:detail', args=[self.board.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(self.client.get(board_url(self.board)).status_code, 200)
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, self.card.pk]),
            {'column_id': self.card.sub_board.columns.get(position=2).pk}, HTTP_X_REQUESTED_WITH='fetch',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(reverse('boards:create')).status_code, 403)

    def test_every_route_of_a_foreign_board_is_403_before_the_method(self):
        self.client.force_login(self.otk)
        for name, args in self.foreign_routes():
            url = reverse(name, args=args)
            with self.subTest(route=name):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(
                    self.client.post(url, {'text': 'x', 'name': 'x', 'direction': 'right'}).status_code, 403,
                )
        self.assertEqual(stage_of(self.foreign_card), 'TODO')
        self.assertEqual(self.foreign.sub_boards.get().name, 'Основная')
        self.assertEqual(self.foreign_sub.columns.count(), 4)

    def test_json_routes_answer_json_403(self):
        self.client.force_login(self.otk)
        response = self.client.get(fragment_url(self.foreign))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response['Content-Type'], 'application/json')
        response = self.client.post(
            reverse('boards:card_move', args=[self.foreign.pk, self.foreign_card.pk]),
            {'column_id': self.foreign_card.sub_board.columns.get(position=2).pk},
            HTTP_X_REQUESTED_WITH='fetch',
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['ok'], False)

    def test_somebody_on_no_board_is_told_so_and_reads_no_board(self):
        self.client.force_login(self.loner)
        response = self.client.get(reverse('boards:list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Вас пока не добавили ни на одну доску')
        self.assertNotContains(response, 'Доска ОТК')
        self.assertNotContains(response, reverse('boards:create'))
        self.assertEqual(self.client.get(board_url(self.board)).status_code, 403)

    def test_the_left_panel_lists_only_readable_boards(self):
        # Was the registry's «Мои»/«Все»: the same answer, on every page.
        self.client.force_login(self.otk)
        content = self.client.get(board_url(self.board)).content.decode()
        nav = content.split('class="board-nav"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('Доска ОТК', nav)
        self.assertNotIn('Чужая доска', nav)
        self.client.force_login(self.admin)
        content = self.client.get(board_url(self.board)).content.decode()
        nav = content.split('class="board-nav"', 1)[1].split('</nav>', 1)[0]
        self.assertIn('Чужая доска', nav)


class NavigationTests(AccessFixture, TestCase):
    def test_menu_and_dashboard(self):
        boards_url = reverse('boards:list')
        for user, count in ((self.otk, 2), (self.admin, 2), (self.loner, 0)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse('dashboard:home'))
                # The sidebar link and the «Быстрый доступ» card.
                self.assertContains(response, f'href="{boards_url}"', count=count)


class TaskVisibilityTests(AccessFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.task = task_of(self.card)
        self.foreign_task = task_of(self.foreign_card)

    def registry(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse('tasks:list'), params)

    def test_a_member_sees_the_tasks_of_their_boards_only(self):
        for tab in ('my', 'all'):
            with self.subTest(tab=tab):
                content = self.registry(self.otk, tab=tab).content.decode()
                self.assertIn('Карточка ОТК', content)
                self.assertNotIn('Карточка ПДО', content)
        content = self.registry(self.loner, tab='all').content.decode()
        self.assertNotIn('Карточка ОТК', content)
        content = self.registry(self.admin, tab='all').content.decode()
        self.assertIn('Карточка ОТК', content)
        self.assertIn('Карточка ПДО', content)

    def test_the_task_page(self):
        self.client.force_login(self.otk)
        self.assertEqual(self.client.get(reverse('tasks:detail', args=[self.foreign_task.pk])).status_code, 404)
        response = self.client.get(reverse('tasks:detail', args=[self.task.pk]))
        self.assertRedirects(
            response, f"{board_url(self.board)}?card={self.card.pk}",
            fetch_redirect_response=False,
        )

    def test_the_type_filter_offers_the_board_to_whoever_uses_boards(self):
        option = '<option value="BOARD"'
        self.assertContains(self.registry(self.otk, tab='all'), option)
        self.assertNotContains(self.registry(self.loner, tab='all'), option)

    def test_excel_export_and_search(self):
        response = self.registry(self.otk, tab='all', export='xlsx')
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            text = ''.join(
                archive.read(name).decode('utf-8') for name in archive.namelist() if name.endswith('.xml')
            )
        self.assertIn('Карточка ОТК', text)
        self.assertNotIn('Карточка ПДО', text)
        self.client.force_login(self.otk)
        response = self.client.get(reverse('dashboard:search'), {'q': 'Карточка ПДО'})
        self.assertNotContains(response, f'Задача №{self.foreign_task.pk}')

    def test_dashboard_counts(self):
        self.assertEqual(my_work_counts(self.otk)['tasks'], 1)
        self.assertEqual(my_work_counts(self.loner)['tasks'], 0)

    def _registry_queries(self):
        self.client.force_login(self.otk)
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse('tasks:list'), {'tab': 'all'})
        return len(queries)

    def test_the_query_count_does_not_grow_with_the_boards(self):
        baseline = self._registry_queries()
        for index in range(3):
            board = create_board(
                code=fresh_code(),
                name=f'Ещё {index}', owner=self.admin, actor=self.admin,
                member_ids=[self.otk.pk, self.pdo.pk],
            )
            new_card(board, self.admin, f'Своя {index}', assignees=[self.otk])
            new_card(self.foreign, self.admin, f'Чужая {index}', assignees=[self.pdo])
        self.assertEqual(self._registry_queries(), baseline)


class MembershipTests(AccessFixture, TestCase):
    def test_any_active_employee_may_be_a_member(self):
        offered = set(BoardForm(owner=self.admin).fields['members'].queryset)
        self.assertLessEqual({self.otk, self.pdo, self.loner, self.admin_two}, offered)
        candidates = set(AddMembersForm(board=self.board).fields['users'].queryset)
        self.assertIn(self.loner, candidates)
        self.assertNotIn(self.otk, candidates)
        self.assertEqual(set(CardForm(board=self.board).fields['assignees'].queryset), {self.admin, self.otk})

    def test_services_take_any_active_employee_and_refuse_an_inactive_one(self):
        added = add_board_members(self.board, [self.loner.pk], actor=self.admin)
        self.assertEqual(added, [self.loner.pk])
        inactive = make_user('access_gone')
        inactive.is_active = False
        inactive.save()
        with self.assertRaises(BoardError):
            add_board_members(self.board, [inactive.pk], actor=self.admin)
        with self.assertRaises(BoardError):
            create_board(code=fresh_code(), name='Новая', owner=self.admin, actor=self.admin, member_ids=[inactive.pk])

    def test_a_new_card_task_names_no_department(self):
        self.assertIsNone(task_of(self.card).department)
        self.client.force_login(self.otk)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(response, 'Карточка ОТК')


class BoardFormTests(AccessFixture, TestCase):
    def post(self, data):
        self.client.force_login(self.admin)
        return self.client.post(reverse('boards:create'), data)

    def test_the_form_asks_for_a_name_and_members_only(self):
        self.client.force_login(self.admin)
        content = self.client.get(reverse('boards:create')).content.decode()
        form = content.split('class="form-card board-form"', 1)[1].split('</form>', 1)[0]
        self.assertIn('name="name"', form)
        self.assertIn('name="members"', form)
        self.assertIn('data-employee-picker', form)
        self.assertIn('data-department-select', form)
        self.assertNotIn('name="description"', form)
        self.assertNotIn('name="department"', form)
        # The owner is never offered in the rows.
        self.assertIn(f'data-employee-picker-exclude="{self.admin.pk}"', form)

    def test_a_name_and_a_code_create_a_board(self):
        response = self.post({'name': 'Только название', 'code': 'only'})
        board = Board.objects.get(name='Только название')
        self.assertRedirects(
            response, reverse('boards:detail', args=[board.pk]), fetch_redirect_response=False,
        )
        self.assertIsNone(board.department_id)
        self.assertEqual(board.description, '')
        self.assertEqual(list(board.members.values_list('user_id', flat=True)), [self.admin.pk])

    def test_members_from_different_departments_repeats_and_the_creator(self):
        UserProfile.objects.filter(user=self.pdo).update(department=other_department())
        response = self.post({
            'name': 'Сводная',
            'code': 'СВД',
            # Two departments, a repeat, an empty row and the creator himself.
            'members': [str(self.otk.pk), str(self.pdo.pk), str(self.otk.pk), '', str(self.admin.pk)],
        })
        board = Board.objects.get(name='Сводная')
        self.assertRedirects(
            response, reverse('boards:detail', args=[board.pk]), fetch_redirect_response=False,
        )
        self.assertEqual(
            sorted(board.members.values_list('user_id', flat=True)),
            sorted([self.admin.pk, self.otk.pk, self.pdo.pk]),
        )
        self.assertEqual(board.owner, self.admin)

    def test_an_inactive_user_sent_by_hand_is_refused(self):
        inactive = make_user('access_form_gone')
        inactive.is_active = False
        inactive.save()
        response = self.post({'name': 'Не создастся', 'members': [str(inactive.pk)]})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Board.objects.filter(name='Не создастся').exists())

    def test_one_more_row_without_javascript(self):
        response = self.post({'name': 'Черновик', 'members': [str(self.otk.pk)], 'add_member_row': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Board.objects.filter(name='Черновик').exists())
        self.assertEqual(len(response.context['member_rows']), 2)
        self.assertEqual(response.context['member_rows'][0]['user'], str(self.otk.pk))
        self.assertContains(response, 'value="Черновик"')

    def test_the_members_page_adds_through_the_same_rows(self):
        self.client.force_login(self.admin)
        content = self.client.get(reverse('boards:members', args=[self.board.pk])).content.decode()
        self.assertIn('data-employee-picker', content)
        self.assertIn('name="users"', content)
        response = self.client.post(
            reverse('boards:members_add', args=[self.board.pk]),
            {'users': [str(self.loner.pk), str(self.loner.pk), '']},
        )
        self.assertRedirects(response, reverse('boards:members', args=[self.board.pk]))
        self.assertTrue(BoardMember.objects.filter(board=self.board, user=self.loner).exists())


class RealtimeTests(AccessFixture, TestCase):
    def test_board_targets_are_full_access_and_this_boards_members(self):
        inactive = make_user('access_member_off', UserProfile.Role.OTK)
        BoardMember.objects.create(board=self.board, user=inactive)
        inactive.is_active = False
        inactive.save()
        targets = {target.identifier for target in board_targets(self.board)}
        self.assertEqual(targets, {self.admin.pk, self.admin_two.pk, self.superuser.pk, self.otk.pk})
        targets = {target.identifier for target in board_targets(self.foreign)}
        self.assertEqual(targets, {self.admin.pk, self.admin_two.pk, self.superuser.pk, self.pdo.pk})

    def test_a_members_revision_moves_only_with_their_boards(self):
        def revision(user):
            return build_sync_state(fresh(user))['revisions'][REVISION_BOARDS]

        otk, loner, admin = revision(self.otk), revision(self.loner), revision(self.admin)
        new_card(self.foreign, self.admin, 'Ещё чужая', assignees=[self.pdo])
        self.assertEqual(revision(self.otk), otk)
        self.assertEqual(revision(self.loner), loner)
        self.assertNotEqual(revision(self.admin), admin)
        new_card(self.board, self.admin, 'Ещё своя', assignees=[self.otk])
        self.assertNotEqual(revision(self.otk), otk)
        self.assertEqual(revision(self.loner), loner)
