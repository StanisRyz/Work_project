"""Stage 6: cancelling a card, the archive shelf, the board filters, the card version."""

import re
from datetime import timedelta

from django.db import connection
from django.http import QueryDict
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from realtime.events import RealtimeEventType
from realtime.fragments import content_revision
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.selectors import build_task_list_state
from tasks.services import TaskWorkflowError, cancel_board_card_task, complete_task

from ..models import Board, BoardCard
from ..permissions import can_cancel_card, can_manage_board, can_restore_board, can_work_on_board
from ..selectors import (
    BoardFilters,
    build_board_nav,
    build_board_state,
    column_counts,
    parse_board_filters,
)
from ..services import (
    BoardError,
    StaleCardError,
    add_board_members,
    archive_board,
    cancel_card,
    complete_card,
    create_card,
    create_column,
    create_sub_board,
    delete_column,
    delete_sub_board,
    move_card,
    move_column,
    move_sub_board,
    rename_column,
    rename_sub_board,
    remove_board_member,
    restore_board,
    update_card,
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    due,
    expected_counts,
    fragment_url,
    make_user,
)


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


def titles(state, name):
    column = next(column for column in state['columns'] if column['name'] == name)
    return [item['card'].title for item in column['cards']]


def counts(state):
    return {str(column['pk']): column['count'] for column in state['columns']}


def main_of(response):
    return response.content.decode().split('<main', 1)[1].split('</main>', 1)[0]


def attribute(content, name):
    match = re.search(rf'{name}="([^"]*)"', content)
    return match.group(1) if match else None


# --------------------------------------------------------------------------
# Cancelling a card
# --------------------------------------------------------------------------


class CancelCardTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        # The member puts the card on the colleague: author ≠ исполнитель.
        self.card_obj = self.card('Ошибочная', assignees=[self.colleague], actor=self.member)

    def test_who_may_cancel(self):
        card = BoardCard.objects.select_related('board').get(pk=self.card_obj.pk)
        self.assertTrue(can_cancel_card(self.member, card), 'автор')
        self.assertTrue(can_cancel_card(self.owner, card), 'владелец')
        self.assertTrue(can_cancel_card(self.admin, card), 'администратор')
        self.assertFalse(can_cancel_card(self.colleague, card), 'исполнитель, не автор')
        self.assertFalse(can_cancel_card(self.outsider, card), 'читатель')
        UserProfile.objects.filter(user=self.owner).update(is_active=False)
        self.owner.refresh_from_db()
        self.assertFalse(can_cancel_card(self.owner, card), 'неактивный владелец')

    def test_cancel_writes_the_record_and_one_event_after_commit(self):
        token = build_sync_state(self.outsider)['revisions'][REVISION_BOARDS]
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                cancel_card(self.card_obj, actor=self.member, reason='  Задвоилась  ')
                self.assertEqual(board_events(publisher), [])
            events = board_events(publisher)
        self.assertEqual([event.data['change'] for event in events], ['card_cancelled'])
        self.assertEqual(events[0].data['card_id'], self.card_obj.pk)
        task = task_of(self.card_obj)
        self.assertEqual(task.status.code, 'CANCELLED')
        self.assertEqual(task.cancellation_reason, 'Задвоилась')
        self.assertEqual(task.cancelled_by, self.member)
        self.assertIsNotNone(task.cancelled_at)
        self.assertIsNone(task.completed_by)
        self.assertEqual(task.execution_comment, '')
        self.assertNotEqual(build_sync_state(self.outsider)['revisions'][REVISION_BOARDS], token)
        # Off every column, but still read by its panel.
        state = build_board_state(self.board, self.main, self.member, card_id=self.card_obj.pk)
        self.assertTrue(all('Ошибочная' not in titles(state, c['name']) for c in state['columns']))
        self.assertEqual(state['card']['task'].status.code, 'CANCELLED')
        self.assertFalse(state['card']['can_cancel'])
        # In the task registry's «Архив», not in «Мои».
        archive = build_task_list_state(self.colleague, QueryDict('tab=archive'))
        self.assertIn(task.pk, [row['task'].pk for row in archive['rows']])
        mine = build_task_list_state(self.colleague, QueryDict('tab=my'))
        self.assertNotIn(task.pk, [row['task'].pk for row in mine['rows']])

    def test_refusals_emit_nothing(self):
        done = self.card('Готовая', assignees=[self.member])
        complete_task(task_of(done), self.member, 'Да')
        for card, actor, reason in (
            (self.card_obj, self.member, '   '),
            (self.card_obj, self.colleague, 'Не моя'),
            (self.card_obj, self.outsider, 'Чужая'),
            (done, self.member, 'Поздно'),
        ):
            with self.subTest(actor=actor.username, card=card.title, reason=reason):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        with self.assertRaises(BoardError):
                            cancel_card(card, actor=actor, reason=reason)
                    self.assertEqual(board_events(publisher), [])
        self.assertEqual(task_of(self.card_obj).status.code, 'IN_PROGRESS')
        self.assertEqual(task_of(done).status.code, 'COMPLETED')

    def test_the_task_service_alone(self):
        with self.assertRaises(TaskWorkflowError):
            cancel_board_card_task(task_of(self.card_obj), actor=self.member, reason='')
        smk_like = Task.objects.exclude(source_type=Task.SourceType.BOARD).first()
        if smk_like is not None:
            with self.assertRaises(TaskWorkflowError):
                cancel_board_card_task(smk_like, actor=self.admin, reason='x')

    def test_view_asks_before_the_method_and_posts_the_reason(self):
        url = reverse('boards:card_cancel', args=[self.board.pk, self.card_obj.pk])
        self.client.force_login(self.colleague)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {'cancellation_reason': 'x'}).status_code, 403)
        self.client.force_login(self.member)
        response = self.client.post(f'{url}?mine=1', {'cancellation_reason': ''})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Укажите причину отмены.')
        response = self.client.post(f'{url}?mine=1', {'cancellation_reason': 'Ошибка ввода'})
        self.assertRedirects(
            response,
            f"{board_url(self.board)}?card={self.card_obj.pk}&mine=1",
            fetch_redirect_response=False,
        )
        self.assertEqual(task_of(self.card_obj).cancellation_reason, 'Ошибка ввода')
        panel = main_of(self.client.get(response['Location']))
        self.assertIn('Ошибка ввода', panel)

    def test_the_button_is_drawn_for_those_who_may(self):
        page = board_url(self.board)
        for user, expected in ((self.member, True), (self.owner, True), (self.colleague, False)):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                content = main_of(self.client.get(page, {'card': self.card_obj.pk}))
                self.assertEqual('data-confirm-comment-name="cancellation_reason"' in content, expected)


# --------------------------------------------------------------------------
# The archive shelf
# --------------------------------------------------------------------------


class ArchiveBoardTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.open_card = self.card('Открытая')

    def close_everything(self):
        complete_task(task_of(self.open_card), self.member, 'Готово')

    def test_refused_while_cards_are_open(self):
        self.card('Ещё одна')
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaisesMessage(BoardError, 'открытые карточки: 2'):
                    archive_board(self.board, actor=self.owner)
            self.assertEqual(board_events(publisher), [])
        self.assertEqual(Board.objects.get(pk=self.board.pk).status, Board.Status.ACTIVE)

    def test_archive_and_restore(self):
        self.close_everything()
        with self.assertRaises(BoardError):
            archive_board(self.board, actor=self.member)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                board = archive_board(self.board, actor=self.owner)
            self.assertEqual([e.data['change'] for e in board_events(publisher)], ['board_archived'])
        self.assertEqual(board.status, Board.Status.ARCHIVED)
        self.assertEqual(board.archived_by, self.owner)
        self.assertIsNotNone(board.archived_at)
        self.assertFalse(can_work_on_board(self.admin, board))
        self.assertFalse(can_manage_board(self.owner, board))
        self.assertTrue(can_restore_board(self.owner, board))
        self.assertFalse(can_restore_board(self.member, board))
        with self.assertRaises(BoardError):
            archive_board(board, actor=self.owner)
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                board = restore_board(board, actor=self.admin)
            self.assertEqual([e.data['change'] for e in board_events(publisher)], ['board_restored'])
        self.assertEqual(board.status, Board.Status.ACTIVE)
        self.assertIsNone(board.archived_at)
        self.assertIsNone(board.archived_by)
        with self.assertRaises(BoardError):
            restore_board(board, actor=self.owner)

    def test_every_write_is_refused_on_an_archived_board(self):
        self.close_everything()
        archived = archive_board(self.board, actor=self.owner)
        newcomer = make_user('lifecycle_newcomer')
        writes = {
            'create': lambda: create_card(
                self.main, actor=self.admin, title='X', due_date=due(), assignee_ids=[self.member.pk],
            ),
            'update': lambda: update_card(
                self.open_card, actor=self.admin, title='X', description='', due_date=due(),
                assignee_ids=[self.member.pk],
            ),
            'move': lambda: move_card(self.open_card, actor=self.admin, column=self.column('REVIEW')),
            'create_sub_board': lambda: create_sub_board(archived, actor=self.admin, name='Новая'),
            'rename_sub_board': lambda: rename_sub_board(self.main, actor=self.admin, name='Другое'),
            'move_sub_board': lambda: move_sub_board(self.main, actor=self.admin, direction='right'),
            'delete_sub_board': lambda: delete_sub_board(self.main, actor=self.admin),
            'create_column': lambda: create_column(self.main, actor=self.admin, name='Новая'),
            'rename_column': lambda: rename_column(self.column('REVIEW'), actor=self.admin, name='Х'),
            'move_column': lambda: move_column(self.column('REVIEW'), actor=self.admin, direction='left'),
            'delete_column': lambda: delete_column(self.column('REVIEW'), actor=self.admin),
            'cancel': lambda: cancel_card(self.open_card, actor=self.admin, reason='x'),
            'add_members': lambda: add_board_members(archived, [newcomer.pk], actor=self.admin),
            'remove_member': lambda: remove_board_member(archived, self.colleague, actor=self.admin),
        }
        for name, write in writes.items():
            with self.subTest(write=name):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        with self.assertRaises(BoardError):
                            write()
                    self.assertEqual(board_events(publisher), [])

    def test_every_route_answers_403_before_the_method(self):
        self.close_everything()
        archive_board(self.board, actor=self.owner)
        pk, card = self.board.pk, self.open_card.pk
        sub, column = self.main.pk, self.column('REVIEW').pk
        routes = [
            reverse('boards:card_create', args=[pk, sub]),
            reverse('boards:sub_board_create', args=[pk, sub]),
            reverse('boards:sub_board_rename', args=[pk, sub]),
            reverse('boards:sub_board_move', args=[pk, sub]),
            reverse('boards:sub_board_delete', args=[pk, sub]),
            reverse('boards:column_create', args=[pk, sub]),
            reverse('boards:column_rename', args=[pk, sub, column]),
            reverse('boards:column_move', args=[pk, sub, column]),
            reverse('boards:column_delete', args=[pk, sub, column]),
            reverse('boards:card_update', args=[pk, card]),
            reverse('boards:card_move', args=[pk, card]),
            reverse('boards:card_cancel', args=[pk, card]),
            reverse('boards:members_add', args=[pk]),
            reverse('boards:member_remove', args=[pk, self.colleague.pk]),
            reverse('boards:archive', args=[pk]),
        ]
        self.client.force_login(self.admin)
        for url in routes:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, {}).status_code, 403)

    def test_the_page_reads_at_the_same_address(self):
        self.close_everything()
        archive_board(self.board, actor=self.owner)
        self.client.force_login(self.owner)
        content = main_of(self.client.get(board_url(self.board)))
        self.assertIn('В архиве', content)
        self.assertIn(reverse('boards:restore', args=[self.board.pk]), content)
        for marker in (
            reverse('boards:archive', args=[self.board.pk]), '+ Карточка', 'data-card-movable',
            reverse('boards:rename', args=[self.board.pk]), '+ Колонка',
            reverse('boards:sub_board_create', args=[self.board.pk, self.main.pk]),
            reverse('boards:column_create', args=[self.board.pk, self.main.pk]),
        ):
            self.assertNotIn(marker, content)

    def test_routes(self):
        url = reverse('boards:archive', args=[self.board.pk])
        self.client.force_login(self.member)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.owner)
        response = self.client.post(url, follow=True)
        self.assertContains(response, 'Сначала завершите или отмените открытые карточки: 1.')
        self.close_everything()
        self.client.post(url)
        self.assertEqual(Board.objects.get(pk=self.board.pk).status, Board.Status.ARCHIVED)
        self.client.post(reverse('boards:restore', args=[self.board.pk]))
        self.assertEqual(Board.objects.get(pk=self.board.pk).status, Board.Status.ACTIVE)

    def test_the_archive_is_its_own_part_of_the_left_panel(self):
        # Was the registry's «Архив» tab: the archived board leaves the live
        # list and is listed under «Архив (N)», still readable by its member.
        self.close_everything()
        archive_board(self.board, actor=self.owner)
        nav = build_board_nav(self.member)
        self.assertEqual(nav['boards'], [])
        self.assertEqual([board.name for board in nav['archived']], ['Планирование'])
        self.client.force_login(self.member)
        content = self.client.get(board_url(self.board)).content.decode()
        self.assertIn('Архив (1)', content)
        self.assertIn('<details class="board-nav__archive" open>', content)


# --------------------------------------------------------------------------
# Filters
# --------------------------------------------------------------------------


class BoardFilterTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        past = timezone.localdate() - timedelta(days=2)
        self.mine_late = self.card('Alpha отчёт', assignees=[self.member], due_date=past)
        self.mine_ok = self.card('Beta план', assignees=[self.member, self.colleague])
        self.theirs_late = self.card('Gamma отчёт', assignees=[self.colleague], due_date=past, stage='REVIEW')
        self.done_mine_late = self.card('Delta отчёт', assignees=[self.member], due_date=past)
        complete_task(task_of(self.done_mine_late), self.member, 'Да')
        self.done_theirs = self.card('Epsilon', assignees=[self.colleague])
        complete_task(task_of(self.done_theirs), self.colleague, 'Да')

    def state(self, **filters):
        return build_board_state(self.board, self.main, self.member, filters=BoardFilters(**filters))

    def visible(self, state):
        return sorted(title for column in state['columns'] for title in titles(state, column['name']))

    def test_each_filter_and_their_combination(self):
        self.assertEqual(len(self.visible(self.state())), 5)
        self.assertEqual(
            self.visible(self.state(mine=True)), ['Alpha отчёт', 'Beta план', 'Delta отчёт'],
        )
        # «Просроченные» narrows open work only; «Готово» keeps its cards.
        self.assertEqual(
            self.visible(self.state(overdue=True)),
            ['Alpha отчёт', 'Delta отчёт', 'Epsilon', 'Gamma отчёт'],
        )
        self.assertEqual(
            self.visible(self.state(q='ОТЧЁТ'.lower())), ['Alpha отчёт', 'Delta отчёт', 'Gamma отчёт'],
        )
        self.assertEqual(self.visible(self.state(q='ALPHA')), ['Alpha отчёт'], 'без учёта регистра')
        self.assertEqual(
            self.visible(self.state(mine=True, overdue=True, q='отчёт')),
            ['Alpha отчёт', 'Delta отчёт'],
        )

    def test_counts_are_the_filtered_numbers_and_match_the_drag_answer(self):
        filters = BoardFilters(mine=True)
        state = build_board_state(self.board, self.main, self.member, filters=filters)
        self.assertEqual(counts(state), expected_counts(self.board, TODO=2, DONE=1))
        self.assertEqual(column_counts(self.main, self.member, filters), counts(state))
        self.assertEqual(
            column_counts(self.main, self.member, BoardFilters(overdue=True)),
            counts(self.state(overdue=True)),
        )

    def test_parsing(self):
        self.assertEqual(
            parse_board_filters({'mine': '1', 'overdue': 'yes', 'q': '  план  '}),
            BoardFilters(mine=True, overdue=False, q='план'),
        )
        self.assertEqual(BoardFilters(mine=True, q='a b').query, 'mine=1&q=a+b')
        self.assertEqual(BoardFilters().query, '')

    def test_page_and_fragment_agree_under_a_filter(self):
        self.client.force_login(self.member)
        query = {'card': self.mine_ok.pk, 'mine': '1', 'q': 'план'}
        page = self.client.get(board_url(self.board), query).content.decode()
        fragment = self.client.get(fragment_url(self.board), query).json()
        self.assertEqual(attribute(page, 'data-columns-revision'), fragment['columns_revision'])
        self.assertEqual(attribute(page, 'data-panel-revision'), fragment['panel_revision'])
        self.assertEqual(fragment['columns_revision'], content_revision(fragment['columns_html']))
        self.assertIn('Beta план', fragment['columns_html'])
        self.assertNotIn('Alpha', fragment['columns_html'])
        self.assertEqual(
            attribute(page, 'data-board-fragment-url'),
            f"{fragment_url(self.board)}?card={self.mine_ok.pk}&amp;mine=1&amp;q=%D0%BF%D0%BB%D0%B0%D0%BD",
        )

    def test_links_keep_the_filter(self):
        self.client.force_login(self.member)
        content = main_of(self.client.get(
            board_url(self.board), {'card': self.mine_ok.pk, 'mine': '1'},
        ))
        page = board_url(self.board)
        self.assertIn(f'href="{page}?card={self.mine_late.pk}&amp;mine=1"', content, 'плитка')
        self.assertIn(f'href="{page}?mine=1" aria-label="Закрыть панель"', content, '«Закрыть»')
        self.assertIn(
            f'data-card-move-url="{reverse("boards:card_move", args=[self.board.pk, self.mine_ok.pk])}?mine=1"',
            content,
        )
        self.assertIn(f'action="{reverse("boards:card_complete", args=[self.board.pk, self.mine_ok.pk])}?mine=1"', content)
        self.assertIn(f'href="{page}?card={self.mine_ok.pk}">Сбросить</a>', content)

    def test_a_redirect_after_a_post_keeps_the_filter(self):
        self.client.force_login(self.member)
        url = reverse('boards:card_move', args=[self.board.pk, self.mine_ok.pk])
        response = self.client.post(f'{url}?mine=1&overdue=1', {'column_id': self.column('REVIEW').pk})
        self.assertRedirects(
            response,
            f"{board_url(self.board)}?card={self.mine_ok.pk}&mine=1&overdue=1",
            fetch_redirect_response=False,
        )

    def test_drag_json_counts_follow_the_filter(self):
        self.client.force_login(self.member)
        url = reverse('boards:card_move', args=[self.board.pk, self.mine_ok.pk])
        answer = self.client.post(
            f'{url}?mine=1', {'column_id': self.column('IN_PROGRESS').pk}, HTTP_X_REQUESTED_WITH='fetch',
        ).json()
        self.assertEqual(answer['counts'], expected_counts(self.board, TODO=1, IN_PROGRESS=1, DONE=1))

    def test_before_a_visible_card_lands_before_it_in_the_full_column(self):
        # TODO holds, in order: Alpha (mine), Beta (mine), the two done cards,
        # Hidden (not mine), Last (mine). Under «Мои» the user sees Alpha,
        # Beta, Last and drops Alpha between Beta and Last: the script posts
        # Last as the next visible card.
        self.card('Hidden', assignees=[self.colleague])
        last = self.card('Last mine', assignees=[self.member])
        move_card(self.mine_late, actor=self.member, column=self.column('TODO'), before_card_id=last.pk)
        order = list(
            BoardCard.objects.filter(column=self.column('TODO'))
            .order_by('position', 'pk').values_list('title', flat=True)
        )
        self.assertEqual(
            order, ['Beta план', 'Delta отчёт', 'Epsilon', 'Hidden', 'Alpha отчёт', 'Last mine'],
        )
        self.assertEqual(
            titles(self.state(mine=True), 'Сделать'), ['Beta план', 'Alpha отчёт', 'Last mine'],
            'what the user saw after the drop',
        )

    def _queries(self, **query):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(board_url(self.board), query)
        return len(queries)

    def test_query_count_is_constant(self):
        self.client.force_login(self.member)
        baseline = self._queries(mine='1', overdue='1', q='о')
        for index in range(5):
            card = self.card(f'о {index}', assignees=[self.member, self.colleague])
            if index % 2:
                complete_task(task_of(card), self.member, 'Да')
        self.assertEqual(self._queries(mine='1', overdue='1', q='о'), baseline)


# --------------------------------------------------------------------------
# The card version
# --------------------------------------------------------------------------


class CardVersionTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.card_obj = self.card('Исходная')

    def edit(self, title, *, version, actor=None, due_date=None):
        return update_card(
            self.card_obj, actor=actor or self.member, title=title, description='',
            due_date=due_date or task_of(self.card_obj).due_date,
            assignee_ids=[self.member.pk], expected_version=version,
        )

    def test_two_tabs_the_second_is_refused(self):
        self.assertEqual(self.card_obj.version, 1)
        self.edit('Первая вкладка', version=1)
        with self.assertRaises(StaleCardError):
            self.edit('Вторая вкладка', version=1, actor=self.colleague)
        card = BoardCard.objects.get(pk=self.card_obj.pk)
        self.assertEqual((card.title, card.version), ('Первая вкладка', 2))
        self.edit('Вторая, перечитав', version=2)
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 3)

    def test_an_empty_edit_a_move_and_a_completion_keep_the_version(self):
        self.edit('Исходная', version=1)
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 1)
        move_card(self.card_obj, actor=self.member, column=self.column('REVIEW'))
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 1)
        complete_card(self.card_obj, actor=self.member, execution_comment='Да')
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 1)

    def test_a_deadline_alone_is_a_new_version(self):
        self.edit('Исходная', version=1, due_date=due(9))
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 2)

    def test_no_expected_version_skips_the_check(self):
        self.edit('Новая', version=None)
        self.edit('Ещё новее', version=None)
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).version, 3)

    def test_the_form_carries_the_version_and_the_refusal_keeps_the_input(self):
        url = board_url(self.board)
        self.client.force_login(self.member)
        form = main_of(self.client.get(url, {'card': self.card_obj.pk, 'edit': '1'}))
        self.assertIn('name="version" value="1"', form)
        self.edit('Чужая правка', version=1, actor=self.colleague)
        response = self.client.post(
            reverse('boards:card_update', args=[self.board.pk, self.card_obj.pk]),
            {
                'title': 'Моя правка', 'description': 'Мой текст', 'version': '1',
                'due_date': due().isoformat(), 'assignees': [self.member.pk],
            },
        )
        self.assertEqual(response.status_code, 200)
        content = main_of(response)
        self.assertIn('Карточку изменили, пока вы её редактировали', content)
        self.assertIn('value="Моя правка"', content)
        self.assertIn('Мой текст', content)
        self.assertIn(f'href="{url}?card={self.card_obj.pk}" target="_blank"', content)
        self.assertEqual(BoardCard.objects.get(pk=self.card_obj.pk).title, 'Чужая правка')
