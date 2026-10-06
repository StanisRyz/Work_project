"""Stage 14: the board's code and the card's number — «ZAP-12».

Giving numbers (cancelled cards keep theirs), the database's word on
uniqueness, the code's format and uniqueness whatever the case, changing it,
where the code is shown, and finding a card by it — on the board, in «Задачи»
and in the topbar search, never across a board the reader is not on.
"""

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from accounts.models import UserProfile
from dashboard.search import quick_search
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task

from ..models import Board, BoardCard
from ..services import (
    BoardCodeError,
    BoardError,
    archive_board,
    cancel_card,
    change_board_code,
    clean_board_code,
    create_board,
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    fragment_url,
    fresh_code,
    make_user,
    new_card,
)
from .test_access import AccessFixture


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


class CardNumberTests(BoardFixtureMixin, TestCase):
    def test_numbers_follow_each_other_per_board(self):
        first, second, third = (self.card(f'Карточка {index}') for index in range(3))
        self.assertEqual([first.number, second.number, third.number], [1, 2, 3])
        other = create_board(code='ZAK', name='Другая', owner=self.owner, actor=self.owner)
        self.assertEqual(new_card(other, self.owner, 'Там', assignees=[self.owner]).number, 1)

    def test_a_cancelled_card_keeps_its_number_and_it_is_never_given_again(self):
        first = self.card('Первая')
        second = self.card('Вторая')
        cancel_card(second, actor=self.member, reason='Ошибка')
        cancel_card(first, actor=self.member, reason='Ошибка')
        second.refresh_from_db()
        self.assertEqual(second.number, 2)
        self.assertEqual(self.card('Третья').number, 3)

    def test_numbers_are_unique_per_board_in_the_database(self):
        card = self.card('Первая')
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardCard.objects.create(
                board=self.board, sub_board=card.sub_board, column=card.column, position=1,
                number=card.number, title='Дубль', created_by=self.member,
            )

    def test_the_code_is_the_boards_code_and_the_number(self):
        change_board_code(self.board, actor=self.owner, code='zap')
        card = self.card('Карточка')
        card.refresh_from_db()
        self.assertEqual(card.code, 'ZAP-1')


class BoardCodeTests(BoardFixtureMixin, TestCase):
    def test_format_and_upper_case(self):
        self.assertEqual(clean_board_code('  zap '), 'ZAP')
        self.assertEqual(clean_board_code('пдо'), 'ПДО')
        self.assertEqual(clean_board_code('ё1'), 'Ё1')
        self.assertEqual(clean_board_code('A1B2C3'), 'A1B2C3')
        for value in ('', ' ', 'Z', 'SEVEN77', 'ZA P', 'ZA-1', 'ZA_1', 'Ä1', 'ZAP!'):
            with self.subTest(value=value):
                with self.assertRaises(BoardCodeError):
                    clean_board_code(value)

    def test_create_stores_upper_case_and_refuses_a_taken_code_whatever_the_case(self):
        board = create_board(code='zap', name='Запуск', owner=self.owner, actor=self.owner)
        self.assertEqual(board.code, 'ZAP')
        for code in ('ZAP', 'zap', 'Zap'):
            with self.subTest(code=code):
                with self.assertRaisesMessage(BoardCodeError, 'Код «ZAP» уже занят'):
                    create_board(code=code, name='Ещё', owner=self.owner, actor=self.owner)
        create_board(code='пдо', name='ПДО', owner=self.owner, actor=self.owner)
        with self.assertRaisesMessage(BoardCodeError, 'уже занят'):
            create_board(code='ПДО', name='ПДО 2', owner=self.owner, actor=self.owner)
        self.assertEqual(Board.objects.filter(name__in=['Ещё', 'ПДО 2']).count(), 0)

    def test_the_code_is_required(self):
        with self.assertRaisesMessage(BoardCodeError, 'Укажите код доски'):
            create_board(code='', name='Без кода', owner=self.owner, actor=self.owner)

    def test_owner_and_administrator_change_it_once(self):
        for actor, code in ((self.owner, 'zap'), (self.admin, 'ZAK')):
            with self.subTest(actor=actor.username):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        change_board_code(self.board, actor=actor, code=code)
                self.assertEqual(
                    [event.data['change'] for event in board_events(publisher)], ['structure_changed'],
                )
                self.board.refresh_from_db()
                self.assertEqual(self.board.code, code.upper())

    def test_the_same_code_changes_nothing(self):
        change_board_code(self.board, actor=self.owner, code='ZAP')
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                change_board_code(self.board, actor=self.owner, code=' zap ')
        self.assertEqual(board_events(publisher), [])

    def test_refusals(self):
        create_board(code='ZAK', name='Другая', owner=self.owner, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'владелец или администратор'):
            change_board_code(self.board, actor=self.member, code='NEW')
        with self.assertRaisesMessage(BoardCodeError, 'уже занят'):
            change_board_code(self.board, actor=self.owner, code='zak')
        with self.assertRaises(BoardCodeError):
            change_board_code(self.board, actor=self.owner, code='слишкомдлинный')
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            change_board_code(self.board, actor=self.owner, code='NEW')
        self.board.refresh_from_db()
        self.assertNotIn(self.board.code, ('NEW', 'ZAK'))

    def test_route_asks_the_right_before_the_method(self):
        url = reverse('boards:change_code', args=[self.board.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {'code': 'NEW'}).status_code, 403)
        self.client.force_login(self.owner)
        before = self.board.code
        self.client.get(url)
        self.board.refresh_from_db()
        self.assertEqual(self.board.code, before)
        response = self.client.post(f'{url}?sub={self.main.pk}', {'code': 'new'})
        self.assertRedirects(response, board_url(self.board), fetch_redirect_response=False)
        self.board.refresh_from_db()
        self.assertEqual(self.board.code, 'NEW')

    def test_a_refused_code_comes_back_as_a_message(self):
        create_board(code='ZAK', name='Другая', owner=self.owner, actor=self.owner)
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse('boards:change_code', args=[self.board.pk]), {'code': 'zak'}, follow=True,
        )
        self.assertContains(response, 'Код «ZAK» уже занят другой доской.')

    def test_new_board_form_asks_for_the_code(self):
        create_board(code='ZAK', name='Другая', owner=self.owner, actor=self.owner)
        self.client.force_login(self.owner)
        url = reverse('boards:create')
        page = self.client.get(url)
        self.assertContains(page, 'name="code"')
        self.assertContains(page, 'ZAP-12')
        refused = self.client.post(url, {'name': 'Запуск', 'code': 'zak'})
        self.assertEqual(refused.status_code, 200)
        self.assertEqual(refused.context['form'].errors['code'], ['Код «ZAK» уже занят другой доской.'])
        missing = self.client.post(url, {'name': 'Запуск', 'code': ''})
        self.assertIn('code', missing.context['form'].errors)
        bad = self.client.post(url, {'name': 'Запуск', 'code': 'z p'})
        self.assertIn('code', bad.context['form'].errors)
        response = self.client.post(url, {'name': 'Запуск', 'code': 'zap'})
        board = Board.objects.get(name='Запуск')
        self.assertEqual(board.code, 'ZAP')
        self.assertRedirects(response, reverse('boards:detail', args=[board.pk]), fetch_redirect_response=False)


class CodeDisplayTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        change_board_code(self.board, actor=self.owner, code='ZAP')
        self.board.refresh_from_db()
        self.card_obj = self.card('Запустить заказ')
        self.task = task_of(self.card_obj)

    def test_tile_heading_and_copy_link(self):
        self.client.force_login(self.member)
        page = self.client.get(board_url(self.board), {'card': self.card_obj.pk})
        content = page.content.decode()
        self.assertIn('<span class="board-tile__number" title="Задача №%d">ZAP-1</span>' % self.task.pk, content)
        self.assertIn('Карточка ZAP-1', content)
        link = reverse('boards:detail', args=[self.board.pk]) + f'?card={self.card_obj.pk}'
        self.assertIn(f'href="{link}" data-board-card-link="ZAP-1"', content)
        # The task's own number stays where it was.
        self.assertIn(f'задача №{self.task.pk}', content)
        # The link works without JavaScript: the board leads to the card.
        self.assertRedirects(
            self.client.get(link), board_url(self.board) + f'?card={self.card_obj.pk}',
            fetch_redirect_response=False,
        )

    def test_tasks_registry_names_the_board_and_the_card(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all'})
        self.assertContains(response, 'Доска «Планирование» · ZAP-1')
        self.assertContains(response, f'№{self.task.pk}')

    def test_a_new_code_renames_every_card(self):
        change_board_code(self.board, actor=self.owner, code='ZAK')
        self.client.force_login(self.member)
        fragment = self.client.get(fragment_url(self.board)).json()
        self.assertIn('ZAK-1', fragment['columns_html'])
        self.assertNotIn('ZAP-1', fragment['columns_html'])

    def test_log_names_the_card_where_it_was_created(self):
        self.client.force_login(self.member)
        fragment = self.client.get(fragment_url(self.board), {'card': self.card_obj.pk}).json()
        self.assertIn('Карточка ZAP-1 создана в колонке «Сделать»', fragment['log_html'])


class CodeSearchTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        change_board_code(self.board, actor=self.owner, code='ZAP')
        self.board.refresh_from_db()
        self.first = self.card('Первая')
        self.second = self.card('Вторая')
        self.third = self.card('Узел 47')

    def tiles(self, q):
        self.client.force_login(self.member)
        html = self.client.get(fragment_url(self.board), {'q': q}).json()['columns_html']
        return [title for title in ('Первая', 'Вторая', 'Узел 47') if f'title="{title}"' in html]

    def test_board_filter_finds_by_code_any_case_and_bare_number(self):
        for q in ('ZAP-2', 'zap-2', 'Zap - 2', '2', '№2'):
            with self.subTest(q=q):
                self.assertEqual(self.tiles(q), ['Вторая'])
        self.assertEqual(self.tiles('Перв'), ['Первая'])
        # «47» is no card's number here, but a title holds it.
        self.assertEqual(self.tiles('47'), ['Узел 47'])
        self.assertEqual(self.tiles('XYZ-2'), [])

    def test_tasks_registry_finds_by_code(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': 'zap-2'})
        self.assertEqual([row['task'].pk for row in response.context['rows']], [task_of(self.second).pk])

    def test_quick_search_finds_by_code(self):
        groups = dict(quick_search(self.member, 'zap-2'))
        hits = groups['Задачи']
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, f'Задача №{task_of(self.second).pk} · ZAP-2')


class ForeignCodeTests(AccessFixture, TestCase):
    """With the real access rule: a card of a board one is not on is not found by its code."""

    def setUp(self):
        super().setUp()
        change_board_code(self.foreign, actor=self.admin, code='PDO')
        change_board_code(self.board, actor=self.admin, code='OTK')

    def test_not_in_quick_search_registry_or_board_filter(self):
        self.assertEqual(dict(quick_search(self.otk, 'PDO-1')).get('Задачи', []), [])
        self.assertTrue(dict(quick_search(self.pdo, 'PDO-1'))['Задачи'])
        self.client.force_login(self.otk)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': 'PDO-1'})
        self.assertEqual(list(response.context['rows']), [])
        mine = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': 'otk-1'})
        self.assertEqual([row['task'].board_card_id for row in mine.context['rows']], [self.card.pk])
        html = self.client.get(fragment_url(self.board), {'q': 'PDO-1'}).json()['columns_html']
        self.assertNotIn('Карточка ПДО', html)
        self.assertNotIn('Карточка ОТК', html)


class FreshCodeHelperTests(TestCase):
    def test_codes_are_valid(self):
        owner = make_user('code_owner', UserProfile.Role.ADMIN)
        for _ in range(3):
            board = create_board(code=fresh_code(), name='Доска', owner=owner, actor=owner)
            self.assertEqual(board.code, clean_board_code(board.code))
