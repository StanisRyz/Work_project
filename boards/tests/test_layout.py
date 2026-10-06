"""The boards frame: the left panel, `/work/boards/`, the heading, `rename_board()`."""

import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from realtime.events import RealtimeEventType
from realtime.fragments import content_revision
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.services import complete_task

from ..models import Board, BoardMember
from ..selectors import build_board_nav
from ..services import (
    BoardError,
    archive_board,
    create_board,
    create_sub_board,
    delete_sub_board,
    remove_board_member,
    rename_board,
)
from ..views import LAST_SUB_BOARD_SESSION_KEY
from .helpers import BoardFixtureMixin, board_url, fragment_url, fresh_code, main_sub_board, make_user


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def nav_of(response):
    """The left panel's markup."""
    return response.content.decode().split('class="board-nav"', 1)[1].split('</nav>', 1)[0]


def head_of(response):
    """The board heading's markup."""
    return response.content.decode().split('class="board-head"', 1)[1].split('</header>', 1)[0]


def attribute(content, name):
    match = re.search(rf'{name}="([^"]*)"', content)
    return match.group(1) if match else None


CSRF_INPUT = re.compile(r'<input\b[^>]*\bname="csrfmiddlewaretoken"[^>]*>')


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


class LeftPanelTests(BoardFixtureMixin, TestCase):
    """Runs with full access widened to every role (the helpers' default):
    `test_access.py` keeps the real admission and the member's own boards."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second = create_board(code=fresh_code(), name='Агрегаты', owner=cls.owner, actor=cls.owner)
        cls.shelved = create_board(code=fresh_code(), name='Старая', owner=cls.owner, actor=cls.owner)
        archive_board(cls.shelved, actor=cls.owner)

    def test_live_boards_by_name_and_the_archive_apart(self):
        nav = build_board_nav(self.member)
        self.assertEqual([board.name for board in nav['boards']], ['Агрегаты', 'Планирование'])
        self.assertEqual([board.name for board in nav['archived']], ['Старая'])
        self.assertIsNone(nav['current_id'])
        self.assertEqual(build_board_nav(self.member, self.board)['current_id'], self.board.pk)

    def test_a_member_without_full_access_sees_only_their_own(self):
        # The real admission: a member reads by membership only.
        from unittest import mock

        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            member = type(self.member).objects.get(pk=self.member.pk)
            nav = build_board_nav(member)
            self.assertEqual([board.name for board in nav['boards']], ['Планирование'])
            self.assertEqual(nav['archived'], [])
            admin = type(self.admin).objects.get(pk=self.admin.pk)
            nav = build_board_nav(admin)
            self.assertEqual([board.name for board in nav['boards']], ['Агрегаты', 'Планирование'])
            self.assertEqual([board.name for board in nav['archived']], ['Старая'])

    def test_every_page_draws_the_panel_with_the_current_board(self):
        self.client.force_login(self.owner)
        for url, current in (
            (board_url(self.board), self.board),
            (reverse('boards:members', args=[self.board.pk]), self.board),
            (reverse('boards:create'), None),
        ):
            with self.subTest(url=url):
                nav = nav_of(self.client.get(url))
                for board in (self.board, self.second):
                    link = f'href="{reverse("boards:detail", args=[board.pk])}"'
                    self.assertIn(link, nav)
                active = re.findall(r'board-nav__link text-ellipsis is-active" href="([^"]+)"', nav)
                self.assertEqual(
                    active, [reverse('boards:detail', args=[current.pk])] if current else [],
                )
                self.assertIn('Архив (1)', nav)
                self.assertIn('title="Планирование"', nav)

    def test_plus_only_for_whoever_may_create(self):
        self.client.force_login(self.owner)
        self.assertIn(reverse('boards:create'), nav_of(self.client.get(board_url(self.board))))
        self.client.force_login(self.member)
        self.assertNotIn(reverse('boards:create'), nav_of(self.client.get(board_url(self.board))))

    def test_one_query_whatever_the_number_of_boards(self):
        member = type(self.member).objects.get(pk=self.member.pk)
        build_board_nav(member)  # the roles lent today, read once per request
        with self.assertNumQueries(1):
            build_board_nav(member)

        def page_queries():
            with CaptureQueriesContext(connection) as queries:
                self.client.get(board_url(self.board))
            return len(queries)

        self.client.force_login(self.member)
        baseline = page_queries()
        for index in range(4):
            create_board(code=fresh_code(), name=f'Ещё {index}', owner=self.owner, actor=self.owner, member_ids=[self.member.pk])
        archive_board(create_board(code=fresh_code(), name='В архив', owner=self.owner, actor=self.owner), actor=self.owner)
        self.assertEqual(page_queries(), baseline)


class BoardsAddressTests(BoardFixtureMixin, TestCase):
    url = reverse('boards:list')

    def test_the_last_sub_board_opened(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        self.client.force_login(self.member)
        self.client.get(board_url(self.board, tab))
        self.assertEqual(self.client.session[LAST_SUB_BOARD_SESSION_KEY], tab.pk)
        self.assertRedirects(self.client.get(self.url), board_url(self.board, tab))
        # A fragment is not «opening» a sub-board.
        self.client.get(fragment_url(self.board))
        self.assertEqual(self.client.session[LAST_SUB_BOARD_SESSION_KEY], tab.pk)

    def test_without_one_the_first_board_of_the_panel(self):
        other = create_board(code=fresh_code(), name='Агрегаты', owner=self.owner, actor=self.owner, member_ids=[self.member.pk])
        self.client.force_login(self.member)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('boards:detail', args=[other.pk]), fetch_redirect_response=False)

    def test_a_deleted_sub_board_falls_back_to_the_first_board(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Временная')
        self.client.force_login(self.member)
        self.client.get(board_url(self.board, tab))
        delete_sub_board(tab, actor=self.owner)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('boards:detail', args=[self.board.pk]), fetch_redirect_response=False)
        self.assertNotIn(LAST_SUB_BOARD_SESSION_KEY, self.client.session)

    def test_a_board_no_longer_read_falls_back_to_the_first_board(self):
        from unittest import mock

        other = create_board(code=fresh_code(), name='Агрегаты', owner=self.owner, actor=self.owner, member_ids=[self.colleague.pk])
        with mock.patch('boards.permissions.BOARD_ACCESS_ROLES', frozenset({UserProfile.Role.ADMIN})):
            self.client.force_login(self.colleague)
            self.client.get(board_url(other))
            remove_board_member(other, self.colleague, actor=self.owner)
            response = self.client.get(self.url)
            self.assertRedirects(
                response, reverse('boards:detail', args=[self.board.pk]), fetch_redirect_response=False,
            )

    def test_the_old_registry_query_string_is_harmless(self):
        self.client.force_login(self.member)
        response = self.client.get(self.url, {'tab': 'archive'})
        self.assertEqual(response.status_code, 302)


class EmptyStateTests(TestCase):
    """No board at all: two answers, with the real admission."""

    def test_whoever_may_create_is_offered_to(self):
        admin = make_user('empty_admin', UserProfile.Role.ADMIN)
        self.client.force_login(admin)
        response = self.client.get(reverse('boards:list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Досок пока нет')
        self.assertContains(response, f'href="{reverse("boards:create")}"')

    def test_anybody_else_is_told_they_are_on_no_board(self):
        owner = make_user('empty_owner', UserProfile.Role.ADMIN)
        member = make_user('empty_member', UserProfile.Role.OTK)
        board = create_board(code=fresh_code(), name='Закрытая', owner=owner, actor=owner, member_ids=[member.pk])
        archive_board(board, actor=owner)
        self.client.force_login(member)
        response = self.client.get(reverse('boards:list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Вас пока не добавили ни на одну доску')
        self.assertNotContains(response, reverse('boards:create'))
        # The archive is still there, on the left.
        self.assertIn('Архив (1)', nav_of(response))


class RenameBoardTests(BoardFixtureMixin, TestCase):
    def rename(self, actor, name):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                result = rename_board(self.board, actor=actor, name=name)
            return result, board_events(publisher)

    def test_owner_and_admin_rename_with_one_event(self):
        for actor, name in ((self.owner, '  Планирование ПДО  '), (self.admin, 'План')):
            with self.subTest(actor=actor.username):
                board, events = self.rename(actor, name)
                self.assertEqual(board.name, name.strip())
                self.assertEqual([(event.data['change'], event.data['card_id']) for event in events],
                                 [('structure_changed', None)])

    def test_the_same_name_changes_nothing(self):
        before = Board.objects.get(pk=self.board.pk).updated_at
        board, events = self.rename(self.owner, ' Планирование ')
        self.assertEqual(events, [])
        self.assertEqual(Board.objects.get(pk=self.board.pk).updated_at, before)

    def test_refusals_publish_nothing(self):
        archived = create_board(code=fresh_code(), name='Архивная', owner=self.owner, actor=self.owner)
        archive_board(archived, actor=self.owner)
        for actor, board, name, message in (
            (self.member, self.board, 'Чужое', 'владелец или администратор'),
            (self.outsider, self.board, 'Чужое', 'владелец или администратор'),
            (self.owner, self.board, '   ', 'Укажите название доски'),
            (self.owner, self.board, 'я' * 201, 'не длиннее 200'),
            (self.owner, archived, 'Новое', 'Доска в архиве'),
        ):
            with self.subTest(actor=actor.username, name=name[:10]):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        with self.assertRaisesMessage(BoardError, message):
                            rename_board(board, actor=actor, name=name)
                    self.assertEqual(board_events(publisher), [])
        self.assertEqual(Board.objects.get(pk=self.board.pk).name, 'Планирование')

    def test_route(self):
        url = reverse('boards:rename', args=[self.board.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {'name': 'Чужое'}).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Board.objects.get(pk=self.board.pk).name, 'Планирование')
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        response = self.client.post(f'{url}?sub={tab.pk}', {'name': 'Планирование цехов'})
        self.assertRedirects(response, board_url(self.board, tab))
        self.assertEqual(Board.objects.get(pk=self.board.pk).name, 'Планирование цехов')
        response = self.client.post(url, {'name': ''}, follow=True)
        self.assertContains(response, 'Обязательное поле.')


class HeadingTests(BoardFixtureMixin, TestCase):
    def test_avatars_up_to_five_and_the_rest_counted(self):
        self.client.force_login(self.member)
        head = head_of(self.client.get(board_url(self.board)))
        self.assertEqual(head.count('class="board-avatar"'), 3)
        self.assertNotIn('board-avatar--more', head)
        self.assertIn(f'href="{reverse("boards:members", args=[self.board.pk])}"', head)
        for index in range(4):
            user = make_user(f'heading_member_{index}')
            BoardMember.objects.create(board=self.board, user=user)
        head = head_of(self.client.get(board_url(self.board)))
        self.assertEqual(head.count('class="board-avatar"'), 5)
        self.assertIn('<span class="board-avatar board-avatar--more">+2</span>', head)

    def test_the_board_menu_only_for_whoever_may(self):
        self.client.force_login(self.owner)
        head = head_of(self.client.get(board_url(self.board)))
        for marker in (reverse('boards:rename', args=[self.board.pk]), reverse('boards:archive', args=[self.board.pk])):
            self.assertIn(marker, head)
        self.assertNotIn(reverse('boards:restore', args=[self.board.pk]), head)
        self.client.force_login(self.member)
        head = head_of(self.client.get(board_url(self.board)))
        self.assertNotIn('data-board-menu', head)
        self.assertIn('+ Карточка', head)
        self.assertNotIn('act-back-link', self.client.get(board_url(self.board)).content.decode())

    def test_an_archived_board_offers_only_the_way_back(self):
        archive_board(self.board, actor=self.owner)
        self.client.force_login(self.owner)
        head = head_of(self.client.get(board_url(self.board)))
        self.assertIn('В архиве', head)
        self.assertIn(reverse('boards:restore', args=[self.board.pk]), head)
        for marker in (reverse('boards:rename', args=[self.board.pk]), reverse('boards:archive', args=[self.board.pk]), '+ Карточка'):
            self.assertNotIn(marker, head)
        self.client.force_login(self.member)
        self.assertNotIn('data-board-menu', head_of(self.client.get(board_url(self.board))))

    def test_filters_sit_under_the_tabs_and_reset_only_when_set(self):
        self.client.force_login(self.member)
        content = self.client.get(board_url(self.board)).content.decode()
        tabs, filters, columns = (
            content.index('data-live-board-tabs'), content.index('class="board-filters"'),
            content.index('data-live-board-columns'),
        )
        self.assertLess(tabs, filters)
        self.assertLess(filters, columns)
        self.assertNotIn('board-filters__reset', content)
        self.assertIn('board-filters__reset', self.client.get(board_url(self.board), {'mine': '1'}).content.decode())


class TabsBlockTests(BoardFixtureMixin, TestCase):
    def test_page_and_fragment_draw_the_same_tabs(self):
        create_sub_board(self.board, actor=self.owner, name='Цех')
        card = self.card('Карточка')
        complete_task(task_of(self.card('Готовая')), self.member, 'Да')
        for user in (self.owner, self.member, self.outsider):
            for query in ({}, {'card': card.pk}, {'mine': '1'}):
                with self.subTest(user=user.username, query=query):
                    self.client.force_login(user)
                    page = self.client.get(board_url(self.board), query).content.decode()
                    fragment = self.client.get(fragment_url(self.board), query).json()
                    self.assertIn(CSRF_INPUT.sub('', fragment['tabs_html']), CSRF_INPUT.sub('', page))
                    self.assertEqual(attribute(page, 'data-tabs-revision'), fragment['tabs_revision'])
                    self.assertEqual(fragment['tabs_revision'], content_revision(fragment['tabs_html']))
                    self.assertIn('Цех', fragment['tabs_html'])
                    self.assertNotIn('board-tabs', fragment['columns_html'])

    def test_the_tabs_fingerprint_moves_with_a_tab(self):
        self.client.force_login(self.member)
        before = self.client.get(fragment_url(self.board)).json()
        tab = create_sub_board(self.board, actor=self.owner, name='Новая вкладка')
        after = self.client.get(fragment_url(self.board)).json()
        self.assertNotEqual(after['tabs_revision'], before['tabs_revision'])
        self.assertEqual(after['columns_revision'], before['columns_revision'])
        self.assertIn('Новая вкладка', after['tabs_html'])
        self.assertEqual(main_sub_board(self.board).pk, self.main.pk)
        delete_sub_board(tab, actor=self.owner)
        self.assertEqual(self.client.get(fragment_url(self.board)).json()['tabs_revision'], before['tabs_revision'])
