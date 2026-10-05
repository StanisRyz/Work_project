"""The board pages: rights before the method, the panel, refusals that keep input."""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from references.models import TaskStatus
from tasks.models import Task
from tasks.services import complete_task

from ..models import Board, BoardCard, BoardMember
from ..services import create_board, create_card
from .helpers import BoardFixtureMixin, due, make_user


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def main_of(response):
    """The page's own content, without the topbar (whose «Сообщить об ошибке»
    carries the current URL and a confirm trigger of its own)."""
    return response.content.decode().split('<main', 1)[1].split('</main>', 1)[0]


class BoardViewMixin(BoardFixtureMixin):
    def detail(self, **params):
        return self.client.get(reverse('boards:detail', args=[self.board.pk]), params)

    def card_url(self, card):
        return f"{reverse('boards:detail', args=[self.board.pk])}?card={card.pk}"


class AccessTests(BoardViewMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Карточка')

    def _routes(self):
        board, card = self.board.pk, self.card_obj.pk
        return {
            'create': reverse('boards:create'),
            'members_add': reverse('boards:members_add', args=[board]),
            'member_remove': reverse('boards:member_remove', args=[board, self.colleague.pk]),
            'card_create': reverse('boards:card_create', args=[board]),
            'card_update': reverse('boards:card_update', args=[board, card]),
            'card_move': reverse('boards:card_move', args=[board, card]),
        }

    def test_anonymous_is_sent_to_login(self):
        urls = [
            reverse('boards:list'),
            reverse('boards:detail', args=[self.board.pk]),
            reverse('boards:members', args=[self.board.pk]),
            *self._routes().values(),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse('accounts:login'), response['Location'])

    def test_every_route_without_the_right_is_403_on_get_and_post(self):
        # The outsider may create nothing (ОТК), manages nothing and is no member.
        self.client.force_login(self.outsider)
        for name, url in self._routes().items():
            for method in ('get', 'post'):
                with self.subTest(route=name, method=method):
                    response = getattr(self.client, method)(url)
                    self.assertEqual(response.status_code, 403)

    def test_member_may_not_manage_members(self):
        self.client.force_login(self.member)
        for name in ('members_add', 'member_remove'):
            with self.subTest(route=name):
                self.assertEqual(self.client.post(self._routes()[name]).status_code, 403)

    def test_get_on_a_mutating_route_changes_nothing(self):
        self.client.force_login(self.owner)
        before = (
            Board.objects.count(), BoardMember.objects.count(),
            list(BoardCard.objects.values_list('pk', 'stage', 'title')),
        )
        for name, url in self._routes().items():
            if name == 'create':
                continue
            with self.subTest(route=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 302)
        after = (
            Board.objects.count(), BoardMember.objects.count(),
            list(BoardCard.objects.values_list('pk', 'stage', 'title')),
        )
        self.assertEqual(before, after)

    def test_readers_open_the_pages(self):
        self.client.force_login(self.outsider)
        for url in (
            reverse('boards:list'),
            reverse('boards:detail', args=[self.board.pk]),
            reverse('boards:members', args=[self.board.pk]),
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)


class RegistryTests(BoardViewMixin, TestCase):
    def test_default_tab_is_mine_or_all(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('boards:list')).context['tab'], 'my')
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('boards:list'))
        self.assertEqual(response.context['tab'], 'all')
        self.assertContains(response, 'Планирование')

    def test_tabs(self):
        create_board(name='Чужая', department=self.department, owner=self.owner, actor=self.owner)
        self.client.force_login(self.member)
        mine = self.client.get(reverse('boards:list'), {'tab': 'my'})
        self.assertEqual([board.name for board in mine.context['boards']], ['Планирование'])
        everything = self.client.get(reverse('boards:list'), {'tab': 'all'})
        self.assertEqual([board.name for board in everything.context['boards']], ['Планирование', 'Чужая'])
        self.assertEqual(everything.context['tab_counts'], {'my': 1, 'all': 2})
        self.assertContains(everything, f'data-row-url="{reverse("boards:detail", args=[self.board.pk])}"')

    def test_counts(self):
        self.card('Открытая')
        self.card('Ещё одна')
        complete_task(task_of(self.card('Выполненная')), self.member, 'Да')
        self.client.force_login(self.member)
        board = self.client.get(reverse('boards:list'), {'tab': 'my'}).context['boards'][0]
        # Owner + two members, two open cards — the viewer's own membership
        # must not narrow the counts.
        self.assertEqual(board.member_count, 3)
        self.assertEqual(board.open_card_count, 2)

    def test_create_button_only_for_creators(self):
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('boards:list')), reverse('boards:create'))
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(reverse('boards:list')), reverse('boards:create'))


class CreateBoardViewTests(BoardViewMixin, TestCase):
    def test_creator_becomes_owner_and_member(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('boards:create')).status_code, 200)
        response = self.client.post(reverse('boards:create'), {
            'name': 'Продажи', 'description': '', 'department': self.department.pk,
            'members': [self.member.pk],
        })
        board = Board.objects.get(name='Продажи')
        self.assertRedirects(response, reverse('boards:detail', args=[board.pk]))
        self.assertEqual(board.owner, self.owner)
        self.assertEqual(
            set(board.members.values_list('user_id', flat=True)), {self.owner.pk, self.member.pk},
        )

    def test_invalid_form_keeps_input(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('boards:create'), {'name': 'Без отдела'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Без отдела"')
        self.assertFalse(Board.objects.filter(name='Без отдела').exists())

    def test_role_without_the_right_gets_403(self):
        self.client.force_login(make_user('mas_user', UserProfile.Role.MAS))
        self.assertEqual(self.client.get(reverse('boards:create')).status_code, 403)


class BoardPageTests(BoardViewMixin, TestCase):
    def test_columns_in_order_with_done_and_without_cancelled(self):
        done = self.card('Сделанная')
        complete_task(task_of(done), self.member, 'Да')
        cancelled = self.card('Отменённая')
        Task.objects.filter(pk=task_of(cancelled).pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'),
        )
        self.client.force_login(self.member)
        response = self.detail()
        self.assertEqual(
            [column['label'] for column in response.context['columns']],
            ['Сделать', 'В работе', 'На проверке', 'Готово'],
        )
        self.assertEqual(
            [item['card'].title for item in response.context['columns'][-1]['cards']], ['Сделанная'],
        )
        self.assertNotContains(response, 'Отменённая')

    def test_more_than_fifty_done_says_how_many_more(self):
        for index in range(52):
            complete_task(task_of(self.card(f'Готовая {index}')), self.member, 'Да')
        self.client.force_login(self.member)
        response = self.detail()
        done = response.context['columns'][-1]
        self.assertEqual(len(done['cards']), 50)
        self.assertContains(response, 'и ещё 2')

    def test_reader_sees_no_control(self):
        card = self.card('Карточка')
        self.client.force_login(self.outsider)
        for params in ({}, {'card': card.pk}, {'card': card.pk, 'edit': '1'}, {'new': 'TODO'}):
            with self.subTest(params=params):
                response = self.detail(**params)
                content = main_of(response)
                for marker in ('<form', '?new=', 'edit=1', 'Переместить', '+ Карточка'):
                    self.assertNotIn(marker, content)

    def _page_queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.detail()
        return len(queries)

    def test_query_count_does_not_depend_on_card_count(self):
        self.client.force_login(self.member)
        self.card('Одна')
        complete_task(task_of(self.card('Готовая')), self.member, 'Да')
        baseline = self._page_queries()
        for index in range(8):
            self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
        for index in range(3):
            complete_task(task_of(self.card(f'Готовая {index}')), self.member, 'Да')
        self.assertEqual(self._page_queries(), baseline)


class PanelTests(BoardViewMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Своя', description='Подробности')
        self.client.force_login(self.member)

    def test_own_card_opens_others_do_not(self):
        response = self.detail(card=self.card_obj.pk)
        self.assertEqual(response.context['panel'], 'view')
        self.assertContains(response, 'Подробности')
        other = create_board(
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
        )
        foreign = create_card(
            other, actor=self.owner, title='Чужая', due_date=due(), assignee_ids=[self.owner.pk],
        )
        for value in (foreign.pk, 999999, 'мусор'):
            with self.subTest(card=value):
                response = self.detail(card=value)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.context['panel'])

    def test_edit_and_new_open_forms(self):
        response = self.detail(card=self.card_obj.pk, edit='1')
        self.assertEqual(response.context['panel'], 'edit')
        self.assertContains(response, reverse('boards:card_update', args=[self.board.pk, self.card_obj.pk]))
        self.assertContains(response, 'value="Своя"')
        response = self.detail(new='REVIEW')
        self.assertEqual(response.context['panel'], 'new')
        self.assertContains(response, 'name="stage" value="REVIEW"')
        for value in ('DONE', 'мусор', ''):
            with self.subTest(new=value):
                self.assertIsNone(self.detail(new=value).context['panel'])

    def test_closed_task_has_no_move_and_no_edit(self):
        complete_task(task_of(self.card_obj), self.member, 'Сделано')
        response = self.detail(card=self.card_obj.pk, edit='1')
        self.assertEqual(response.context['panel'], 'view')
        self.assertNotIn('Переместить', main_of(response))
        self.assertNotIn('edit=1', main_of(response))
        self.assertContains(response, reverse('tasks:detail', args=[task_of(self.card_obj).pk]))


class CardRouteTests(BoardViewMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.member)

    def _data(self, **overrides):
        data = {
            'title': 'Позвонить заказчику',
            'description': 'Уточнить сроки',
            'due_date': due(4).isoformat(),
            'assignees': [self.member.pk],
            'stage': 'IN_PROGRESS',
        }
        data.update(overrides)
        return data

    def test_create_redirects_to_the_card(self):
        response = self.client.post(reverse('boards:card_create', args=[self.board.pk]), self._data())
        card = BoardCard.objects.get(title='Позвонить заказчику')
        self.assertRedirects(response, self.card_url(card))
        self.assertEqual(card.stage, 'IN_PROGRESS')
        self.assertEqual(task_of(card).task_text, 'Позвонить заказчику\n\nУточнить сроки')

    def test_invalid_form_keeps_input(self):
        response = self.client.post(
            reverse('boards:card_create', args=[self.board.pk]), self._data(due_date=''),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['panel'], 'new')
        self.assertContains(response, 'value="Позвонить заказчику"')
        self.assertContains(response, 'Уточнить сроки')
        self.assertFalse(BoardCard.objects.exists())

    def test_service_refusal_keeps_input(self):
        # A member deactivated after the page was drawn: the form no longer
        # offers them, so post an id the service itself has to refuse.
        self.colleague.userprofile.is_active = False
        self.colleague.userprofile.save()
        response = self.client.post(
            reverse('boards:card_create', args=[self.board.pk]),
            self._data(assignees=[self.colleague.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="Позвонить заказчику"')
        self.assertFalse(BoardCard.objects.exists())

    def test_service_refusal_message_is_shown(self):
        card = self.card('Карточка')
        complete_task(task_of(card), self.member, 'Сделано')
        response = self.client.post(
            reverse('boards:card_update', args=[self.board.pk, card.pk]),
            self._data(title='После закрытия'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Задача карточки уже закрыта')
        self.assertContains(response, 'value="После закрытия"')
        card.refresh_from_db()
        self.assertEqual(card.title, 'Карточка')

    def test_update_redirects_to_the_card(self):
        card = self.card('Карточка')
        response = self.client.post(
            reverse('boards:card_update', args=[self.board.pk, card.pk]),
            self._data(title='Новое', assignees=[self.colleague.pk]),
        )
        self.assertRedirects(response, self.card_url(card))
        card.refresh_from_db()
        self.assertEqual(card.title, 'Новое')
        self.assertEqual(
            list(task_of(card).assignees.values_list('user_id', flat=True)), [self.colleague.pk],
        )

    def test_move_redirects_and_closed_task_is_refused(self):
        card = self.card('Карточка')
        url = reverse('boards:card_move', args=[self.board.pk, card.pk])
        response = self.client.post(url, {'stage': 'REVIEW'})
        self.assertRedirects(response, self.card_url(card))
        card.refresh_from_db()
        self.assertEqual(card.stage, 'REVIEW')
        complete_task(task_of(card), self.member, 'Сделано')
        response = self.client.post(url, {'stage': 'TODO'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Задача карточки закрыта')
        card.refresh_from_db()
        self.assertEqual(card.stage, 'REVIEW')

    def test_card_of_another_board_is_404(self):
        other = create_board(
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
            member_ids=[self.member.pk],
        )
        foreign = create_card(
            other, actor=self.member, title='Чужая', due_date=due(), assignee_ids=[self.member.pk],
        )
        response = self.client.post(
            reverse('boards:card_move', args=[self.board.pk, foreign.pk]), {'stage': 'REVIEW'},
        )
        self.assertEqual(response.status_code, 404)


class MembersViewTests(BoardViewMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.owner)

    def test_owner_sees_controls_member_does_not(self):
        response = self.client.get(reverse('boards:members', args=[self.board.pk]))
        self.assertContains(response, reverse('boards:members_add', args=[self.board.pk]))
        self.assertContains(response, 'Владелец')
        self.client.force_login(self.member)
        response = self.client.get(reverse('boards:members', args=[self.board.pk]))
        self.assertNotContains(response, reverse('boards:members_add', args=[self.board.pk]))
        self.assertNotIn('data-confirm-url', main_of(response))

    def test_add_and_remove(self):
        response = self.client.post(
            reverse('boards:members_add', args=[self.board.pk]), {'users': [self.outsider.pk]},
        )
        self.assertRedirects(response, reverse('boards:members', args=[self.board.pk]))
        self.assertTrue(BoardMember.objects.filter(board=self.board, user=self.outsider).exists())
        response = self.client.post(
            reverse('boards:member_remove', args=[self.board.pk, self.outsider.pk]),
        )
        self.assertRedirects(response, reverse('boards:members', args=[self.board.pk]))
        self.assertFalse(BoardMember.objects.filter(board=self.board, user=self.outsider).exists())

    def test_owner_and_open_card_assignee_are_refused(self):
        self.card(assignees=[self.colleague])
        for user, text in (
            (self.owner, 'Владельца доски нельзя исключить'),
            (self.colleague, 'Сначала переназначьте карточку'),
        ):
            with self.subTest(user=user.username):
                response = self.client.post(
                    reverse('boards:member_remove', args=[self.board.pk, user.pk]), follow=True,
                )
                self.assertContains(response, text)
                self.assertTrue(BoardMember.objects.filter(board=self.board, user=user).exists())


class NavigationTests(BoardViewMixin, TestCase):
    def test_menu_and_dashboard_for_any_signed_in_user(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse('dashboard:home'))
        self.assertContains(response, f'href="{reverse("boards:list")}"', count=2)
        self.assertContains(response, '#dash-icon-boards')
        response = self.client.get(reverse('boards:list'))
        self.assertContains(response, 'sidebar__category sidebar__category--active" href="/work/boards/"')
