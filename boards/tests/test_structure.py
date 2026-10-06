"""Sub-boards and columns: the services, `card_column()`, the routes, the page."""

from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from references.models import TaskStatus
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.presentation import board_card_url
from tasks.services import complete_task, reopen_task

from ..columns import MAX_COLUMNS, card_column
from ..models import BoardCard, BoardColumn, SubBoard
from ..selectors import build_board_state, sub_board_columns
from ..services import (
    BoardError,
    archive_board,
    cancel_card,
    create_board,
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
)
from .helpers import (
    BoardFixtureMixin,
    board_url,
    column_of,
    done_column_of,
    due,
    new_card,
    stage_of,
)


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def names(sub_board):
    return list(
        BoardColumn.objects.filter(sub_board=sub_board).order_by('position').values_list('name', flat=True)
    )


def tab_names(board):
    return list(SubBoard.objects.filter(board=board).order_by('position').values_list('name', flat=True))


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


class StructureRightsTests(BoardFixtureMixin, TestCase):
    """Every structure operation: the owner and an administrator — yes; a
    member, an outsider and an archived board — no, with no event."""

    def operations(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Запасная')
        column = create_column(self.main, actor=self.owner, name='Запасная')
        return {
            'create_sub_board': lambda actor: create_sub_board(self.board, actor=actor, name=f'Новая {actor.pk}'),
            'rename_sub_board': lambda actor: rename_sub_board(tab, actor=actor, name=f'Имя {actor.pk}'),
            'move_sub_board': lambda actor: move_sub_board(tab, actor=actor, direction='left'),
            'create_column': lambda actor: create_column(self.main, actor=actor, name=f'Колонка {actor.pk}'),
            'rename_column': lambda actor: rename_column(column, actor=actor, name=f'Имя {actor.pk}'),
            'move_column': lambda actor: move_column(column, actor=actor, direction='left'),
        }

    def test_owner_and_admin_may(self):
        for actor in (self.owner, self.admin):
            for name, operation in self.operations().items():
                with self.subTest(actor=actor.username, operation=name):
                    with capture_realtime_events() as publisher:
                        with self.captureOnCommitCallbacks(execute=True):
                            operation(actor)
                        self.assertEqual(
                            [event.data['change'] for event in board_events(publisher)],
                            ['structure_changed'],
                        )
        # And the deletes.
        tab = create_sub_board(self.board, actor=self.owner, name='На удаление')
        delete_sub_board(tab, actor=self.admin)
        column = create_column(self.main, actor=self.owner, name='На удаление')
        delete_column(column, actor=self.admin)

    def test_member_and_outsider_may_not(self):
        operations = self.operations()
        tab = SubBoard.objects.get(board=self.board, name='Запасная')
        column = BoardColumn.objects.get(sub_board=self.main, name='Запасная')
        operations['delete_sub_board'] = lambda actor: delete_sub_board(tab, actor=actor)
        operations['delete_column'] = lambda actor: delete_column(column, actor=actor)
        before = (tab_names(self.board), names(self.main))
        for actor in (self.member, self.colleague, self.outsider):
            for name, operation in operations.items():
                with self.subTest(actor=actor.username, operation=name):
                    with capture_realtime_events() as publisher:
                        with self.captureOnCommitCallbacks(execute=True):
                            with self.assertRaisesMessage(BoardError, 'владелец доски или администратор'):
                                operation(actor)
                        self.assertEqual(board_events(publisher), [])
        self.assertEqual((tab_names(self.board), names(self.main)), before)

    def test_an_archived_board_refuses_everyone(self):
        board = create_board(name='Пустая', owner=self.owner, actor=self.owner)
        tab = board.sub_boards.get()
        archive_board(board, actor=self.owner)
        for actor in (self.owner, self.admin):
            with self.subTest(actor=actor.username):
                with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
                    create_sub_board(board, actor=actor, name='Новая')
                with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
                    create_column(tab, actor=actor, name='Новая')
                with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
                    rename_column(tab.columns.first(), actor=actor, name='Новая')


class SubBoardTests(BoardFixtureMixin, TestCase):
    def test_create_puts_it_last_with_the_default_columns(self):
        tab = create_sub_board(self.board, actor=self.owner, name='  Цех МП  ')
        self.assertEqual(tab.name, 'Цех МП')
        self.assertEqual(tab_names(self.board), ['Основная', 'Цех МП'])
        self.assertEqual(names(tab), ['Сделать', 'В работе', 'На проверке', 'Готово'])
        self.assertEqual(tab.created_by, self.owner)

    def test_names_are_trimmed_required_bounded_and_unique_on_the_board(self):
        for name, message in (
            ('   ', 'Укажите название поддоски'),
            ('я' * 101, 'не длиннее 100'),
            ('Основная', 'уже есть'),
            ('ОСНОВНАЯ', 'уже есть'),
        ):
            with self.subTest(name=name[:20]), self.assertRaisesMessage(BoardError, message):
                create_sub_board(self.board, actor=self.owner, name=name)
        # Another board may have the same name.
        other = create_board(name='Другая', owner=self.owner, actor=self.owner)
        create_sub_board(other, actor=self.owner, name='Цех')
        create_sub_board(self.board, actor=self.owner, name='Цех')

    def test_rename(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        rename_sub_board(tab, actor=self.owner, name='Цех ПиР')
        self.assertEqual(tab_names(self.board), ['Основная', 'Цех ПиР'])
        with self.assertRaisesMessage(BoardError, 'уже есть'):
            rename_sub_board(tab, actor=self.owner, name='основная')
        # Its own name in another case is not a clash with itself.
        rename_sub_board(tab, actor=self.owner, name='ЦЕХ ПИР')
        self.assertEqual(tab_names(self.board), ['Основная', 'ЦЕХ ПИР'])

    def test_move_left_and_right_and_the_edges(self):
        a = create_sub_board(self.board, actor=self.owner, name='А')
        create_sub_board(self.board, actor=self.owner, name='Б')
        move_sub_board(a, actor=self.owner, direction='left')
        self.assertEqual(tab_names(self.board), ['А', 'Основная', 'Б'])
        with self.assertRaisesMessage(BoardError, 'уже первая'):
            move_sub_board(a, actor=self.owner, direction='left')
        move_sub_board(self.main, actor=self.owner, direction='right')
        self.assertEqual(tab_names(self.board), ['А', 'Б', 'Основная'])
        with self.assertRaisesMessage(BoardError, 'уже последняя'):
            move_sub_board(self.main, actor=self.owner, direction='right')
        with self.assertRaises(BoardError):
            move_sub_board(a, actor=self.owner, direction='up')
        self.assertEqual(
            list(SubBoard.objects.filter(board=self.board).order_by('position').values_list('position', flat=True)),
            [1, 2, 3],
        )

    def test_delete_only_an_empty_one_and_never_the_last(self):
        with self.assertRaisesMessage(BoardError, 'единственная поддоска'):
            delete_sub_board(self.main, actor=self.owner)
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        card = new_card(self.board, self.member, 'На вкладке', assignees=[self.member], sub_board=tab)
        cancel_card(card, actor=self.member, reason='Ошибка')
        # Even a cancelled card keeps the tab: it is a record.
        with self.assertRaisesMessage(BoardError, 'в том числе завершённые или отменённые'):
            delete_sub_board(tab, actor=self.owner)
        self.assertIn('Цех', tab_names(self.board))
        empty = create_sub_board(self.board, actor=self.owner, name='Пустая')
        column_ids = list(empty.columns.values_list('pk', flat=True))
        delete_sub_board(empty, actor=self.owner)
        self.assertEqual(tab_names(self.board), ['Основная', 'Цех'])
        self.assertFalse(BoardColumn.objects.filter(pk__in=column_ids).exists())


class ColumnTests(BoardFixtureMixin, TestCase):
    def test_create_goes_before_the_closing_column(self):
        column = create_column(self.main, actor=self.owner, name='  Приёмка ОТК ')
        self.assertEqual(column.name, 'Приёмка ОТК')
        self.assertFalse(column.is_done)
        self.assertEqual(names(self.main), ['Сделать', 'В работе', 'На проверке', 'Приёмка ОТК', 'Готово'])
        self.assertEqual(
            list(self.main.columns.order_by('position').values_list('position', flat=True)), [1, 2, 3, 4, 5],
        )

    def test_names_are_trimmed_required_and_bounded(self):
        for name, message in (('  ', 'Укажите название колонки'), ('я' * 61, 'не длиннее 60')):
            with self.subTest(name=name[:10]):
                with self.assertRaisesMessage(BoardError, message):
                    create_column(self.main, actor=self.owner, name=name)
                with self.assertRaisesMessage(BoardError, message):
                    rename_column(self.column('TODO'), actor=self.owner, name=name)

    def test_no_more_than_fifteen(self):
        for index in range(MAX_COLUMNS - 4):
            create_column(self.main, actor=self.owner, name=f'Этап {index}')
        self.assertEqual(self.main.columns.count(), MAX_COLUMNS)
        with self.assertRaisesMessage(BoardError, f'уже {MAX_COLUMNS} колонок'):
            create_column(self.main, actor=self.owner, name='Лишняя')
        self.assertEqual(self.main.columns.count(), MAX_COLUMNS)
        self.assertEqual(names(self.main)[-1], 'Готово')

    def test_rename_the_closing_one_too(self):
        rename_column(done_column_of(self.board), actor=self.owner, name='Сделано')
        self.assertEqual(names(self.main)[-1], 'Сделано')
        self.assertTrue(done_column_of(self.board).is_done)

    def test_move_among_the_working_columns_only(self):
        review = self.column('REVIEW')
        with self.assertRaisesMessage(BoardError, 'всегда последняя'):
            move_column(review, actor=self.owner, direction='right')
        move_column(review, actor=self.owner, direction='left')
        self.assertEqual(names(self.main), ['Сделать', 'На проверке', 'В работе', 'Готово'])
        move_column(review, actor=self.owner, direction='left')
        with self.assertRaisesMessage(BoardError, 'уже первая'):
            move_column(review, actor=self.owner, direction='left')
        with self.assertRaisesMessage(BoardError, 'всегда последняя'):
            move_column(done_column_of(self.board), actor=self.owner, direction='left')
        self.assertEqual(names(self.main), ['На проверке', 'Сделать', 'В работе', 'Готово'])

    def test_delete_rules(self):
        todo, in_progress, review = self.column('TODO'), self.column('IN_PROGRESS'), self.column('REVIEW')
        with self.assertRaisesMessage(BoardError, 'Завершающую колонку удалить нельзя'):
            delete_column(done_column_of(self.board), actor=self.owner)
        open_card = self.card('Открытая', stage='IN_PROGRESS')
        with self.assertRaisesMessage(BoardError, 'открытые карточки: 1'):
            delete_column(in_progress, actor=self.owner)
        # Closed cards do not hold a column: they keep their record with no column.
        done = self.card('Выполненная', stage='REVIEW')
        complete_task(task_of(done), self.member, 'Да')
        cancelled = self.card('Отменённая', stage='REVIEW')
        cancel_card(cancelled, actor=self.member, reason='Ошибка')
        delete_column(review, actor=self.owner)
        for card in (done, cancelled):
            card.refresh_from_db()
            self.assertIsNone(card.column_id)
        self.assertEqual(names(self.main), ['Сделать', 'В работе', 'Готово'])
        move_card(open_card, actor=self.member, column=todo)
        delete_column(in_progress, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'единственная рабочая колонка'):
            delete_column(todo, actor=self.owner)
        self.assertEqual(names(self.main), ['Сделать', 'Готово'])

    def test_cards_without_a_column_hold_the_first_one(self):
        done = self.card('Была в первой', stage='TODO')
        complete_task(task_of(done), self.member, 'Да')
        delete_column(self.column('TODO'), actor=self.owner)
        reopen_task(task_of(done), self.admin)
        # Reopened with no column: it stands in the new first column, which
        # therefore holds an open card and cannot be deleted under it.
        first = column_of(self.board, 'TODO')
        self.assertEqual(first.name, 'В работе')
        with self.assertRaisesMessage(BoardError, 'открытые карточки: 1'):
            delete_column(first, actor=self.owner)


class CardColumnTests(BoardFixtureMixin, TestCase):
    def column_of_card(self, card):
        card.refresh_from_db()
        return card_column(card, task_of(card), sub_board_columns(card.sub_board))

    def test_open_completed_and_cancelled(self):
        card = self.card('Карточка', stage='IN_PROGRESS')
        self.assertEqual(self.column_of_card(card), self.column('IN_PROGRESS'))
        complete_task(task_of(card), self.member, 'Да')
        self.assertEqual(self.column_of_card(card), done_column_of(self.board))
        reopen_task(task_of(card), self.admin)
        self.assertEqual(self.column_of_card(card), self.column('IN_PROGRESS'))
        cancel_card(card, actor=self.member, reason='Ошибка')
        self.assertIsNone(self.column_of_card(card))

    def test_no_column_is_the_first_working_one(self):
        card = self.card('Карточка', stage='REVIEW')
        BoardCard.objects.filter(pk=card.pk).update(column=None)
        self.assertEqual(self.column_of_card(card), self.column('TODO'))

    def test_reopened_after_its_column_was_deleted(self):
        card = self.card('Карточка', stage='REVIEW')
        complete_task(task_of(card), self.member, 'Да')
        delete_column(self.column('REVIEW'), actor=self.owner)
        self.assertEqual(self.column_of_card(card), done_column_of(self.board))
        state = build_board_state(self.board, self.main, self.admin, card_id=card.pk)
        self.assertEqual(state['card']['return_column'], self.column('TODO'))
        reopen_task(task_of(card), self.admin)
        self.assertEqual(self.column_of_card(card), self.column('TODO'))
        state = build_board_state(self.board, self.main, self.member)
        self.assertEqual([item['card'] for item in state['columns'][0]['cards']], [card])


class CardPlacementTests(BoardFixtureMixin, TestCase):
    def test_create_in_a_working_column_of_its_own_sub_board_only(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        for column, message in (
            (done_column_of(self.board), 'завершите задачу'),
            (column_of(self.board, 'TODO', tab), 'не найдена на этой поддоске'),
            (999999, 'не найдена на этой поддоске'),
        ):
            with self.subTest(column=getattr(column, 'pk', column)):
                with self.assertRaisesMessage(BoardError, message):
                    create_card(
                        self.main, actor=self.member, title='X', due_date=due(),
                        assignee_ids=[self.member.pk], column=column,
                    )
        self.assertFalse(BoardCard.objects.exists())
        card = create_card(
            tab, actor=self.member, title='Без колонки', due_date=due(), assignee_ids=[self.member.pk],
        )
        self.assertEqual((card.board, card.sub_board, card.column), (self.board, tab, column_of(self.board, 'TODO', tab)))

    def test_a_sub_board_of_another_board_is_refused(self):
        other = create_board(name='Другая', owner=self.owner, actor=self.owner, member_ids=[self.member.pk])
        card = create_card(
            other.sub_boards.get(), actor=self.member, title='Там', due_date=due(),
            assignee_ids=[self.member.pk],
        )
        self.assertEqual(card.board, other)

    def test_move_never_leaves_the_sub_board(self):
        card = self.card('Карточка')
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        with self.assertRaisesMessage(BoardError, 'не найдена на этой поддоске'):
            move_card(card, actor=self.member, column=column_of(self.board, 'IN_PROGRESS', tab))
        with self.assertRaisesMessage(BoardError, 'завершите задачу'):
            move_card(card, actor=self.member, column=done_column_of(self.board))
        self.assertEqual(stage_of(card), 'TODO')
        self.assertEqual(card.sub_board, self.main)

    def test_clean_checks_the_three_agree(self):
        card = self.card('Карточка')
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        card.column = column_of(self.board, 'TODO', tab)
        with self.assertRaises(ValidationError):
            card.clean()
        card.column = done_column_of(self.board)
        with self.assertRaises(ValidationError):
            card.clean()
        other = create_board(name='Другая', owner=self.owner, actor=self.owner)
        card.refresh_from_db()
        card.sub_board = other.sub_boards.get()
        with self.assertRaises(ValidationError):
            card.clean()


class StructureRouteTests(BoardFixtureMixin, TestCase):
    def routes(self, tab, column):
        pk = self.board.pk
        return {
            'sub_board_create': (reverse('boards:sub_board_create', args=[pk, tab.pk]), {'name': 'Новая'}),
            'sub_board_rename': (reverse('boards:sub_board_rename', args=[pk, tab.pk]), {'name': 'Другое'}),
            'sub_board_move': (reverse('boards:sub_board_move', args=[pk, tab.pk]), {'direction': 'right'}),
            'sub_board_delete': (reverse('boards:sub_board_delete', args=[pk, tab.pk]), {}),
            'column_create': (reverse('boards:column_create', args=[pk, tab.pk]), {'name': 'Новая'}),
            'column_rename': (reverse('boards:column_rename', args=[pk, tab.pk, column.pk]), {'name': 'Другое'}),
            'column_move': (reverse('boards:column_move', args=[pk, tab.pk, column.pk]), {'direction': 'left'}),
            'column_delete': (reverse('boards:column_delete', args=[pk, tab.pk, column.pk]), {}),
        }

    def snapshot(self):
        return (
            list(SubBoard.objects.order_by('pk').values_list('pk', 'name', 'position')),
            list(BoardColumn.objects.order_by('pk').values_list('pk', 'name', 'position')),
        )

    def test_a_member_gets_403_before_the_method(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        before = self.snapshot()
        self.client.force_login(self.member)
        for name, (url, data) in self.routes(tab, self.column('REVIEW')).items():
            with self.subTest(route=name):
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_a_get_changes_nothing(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        before = self.snapshot()
        self.client.force_login(self.owner)
        for name, (url, _) in self.routes(tab, column_of(self.board, 'REVIEW', tab)).items():
            with self.subTest(route=name):
                self.assertEqual(self.client.get(url).status_code, 302)
        self.assertEqual(self.snapshot(), before)

    def test_the_owner_changes_the_structure(self):
        self.client.force_login(self.owner)
        page = board_url(self.board)
        response = self.client.post(
            reverse('boards:sub_board_create', args=[self.board.pk, self.main.pk]), {'name': 'Цех МП'},
        )
        tab = SubBoard.objects.get(board=self.board, name='Цех МП')
        self.assertRedirects(response, board_url(self.board, tab))
        response = self.client.post(
            reverse('boards:column_create', args=[self.board.pk, self.main.pk]) + '?mine=1',
            {'name': 'Приёмка'},
        )
        self.assertRedirects(response, f'{page}?mine=1', fetch_redirect_response=False)
        self.assertIn('Приёмка', names(self.main))
        column = BoardColumn.objects.get(sub_board=self.main, name='Приёмка')
        self.client.post(reverse('boards:column_rename', args=[self.board.pk, self.main.pk, column.pk]), {'name': 'Контроль'})
        self.client.post(reverse('boards:column_move', args=[self.board.pk, self.main.pk, column.pk]), {'direction': 'left'})
        self.assertEqual(names(self.main), ['Сделать', 'В работе', 'Контроль', 'На проверке', 'Готово'])
        self.client.post(reverse('boards:column_delete', args=[self.board.pk, self.main.pk, column.pk]))
        self.assertNotIn('Контроль', names(self.main))
        response = self.client.post(reverse('boards:sub_board_delete', args=[self.board.pk, tab.pk]))
        self.assertRedirects(response, reverse('boards:detail', args=[self.board.pk]), fetch_redirect_response=False)
        self.assertEqual(tab_names(self.board), ['Основная'])

    def test_a_refusal_is_a_message_on_the_sub_board(self):
        self.card('Открытая')
        self.client.force_login(self.owner)
        url = reverse('boards:column_delete', args=[self.board.pk, self.main.pk, self.column('TODO').pk])
        response = self.client.post(url, follow=True)
        self.assertRedirects(response, board_url(self.board))
        self.assertContains(response, 'В колонке открытые карточки: 1.')
        response = self.client.post(
            reverse('boards:column_create', args=[self.board.pk, self.main.pk]), {'name': '   '}, follow=True,
        )
        self.assertContains(response, 'Обязательное поле.')
        response = self.client.post(
            reverse('boards:sub_board_delete', args=[self.board.pk, self.main.pk]), follow=True,
        )
        self.assertContains(response, 'На поддоске есть карточки')

    def test_a_column_of_another_sub_board_is_404(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        self.client.force_login(self.owner)
        url = reverse('boards:column_rename', args=[self.board.pk, tab.pk, self.column('TODO').pk])
        self.assertEqual(self.client.post(url, {'name': 'X'}).status_code, 404)


class AddressTests(BoardFixtureMixin, TestCase):
    def test_the_board_leads_to_its_first_tab_with_the_query(self):
        self.client.force_login(self.member)
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        move_sub_board(tab, actor=self.owner, direction='left')
        response = self.client.get(reverse('boards:detail', args=[self.board.pk]), {'mine': '1'})
        self.assertRedirects(response, f'{board_url(self.board, tab)}?mine=1')

    def test_a_card_leads_to_its_own_tab(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        card = new_card(self.board, self.member, 'На вкладке', assignees=[self.member], sub_board=tab)
        self.client.force_login(self.member)
        response = self.client.get(reverse('boards:detail', args=[self.board.pk]), {'card': card.pk})
        self.assertRedirects(response, f'{board_url(self.board, tab)}?card={card.pk}')
        self.assertEqual(board_card_url(task_of(card)), f'{board_url(self.board, tab)}?card={card.pk}')
        response = self.client.get(reverse('tasks:detail', args=[task_of(card).pk]))
        self.assertRedirects(response, f'{board_url(self.board, tab)}?card={card.pk}')
        # A redirect after a POST goes to the card's tab, filter kept.
        url = reverse('boards:card_move', args=[self.board.pk, card.pk]) + '?q=x'
        response = self.client.post(url, {'column_id': column_of(self.board, 'REVIEW', tab).pk})
        self.assertRedirects(response, f'{board_url(self.board, tab)}?card={card.pk}&q=x', fetch_redirect_response=False)

    def test_a_tab_of_another_board_is_404(self):
        other = create_board(name='Другая', owner=self.owner, actor=self.owner)
        self.client.force_login(self.member)
        url = reverse('boards:sub_board', args=[self.board.pk, other.sub_boards.get().pk])
        self.assertEqual(self.client.get(url).status_code, 404)


class StructurePageTests(BoardFixtureMixin, TestCase):
    def test_tabs_and_menus_for_the_manager_only(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Цех')
        self.client.force_login(self.owner)
        content = self.client.get(board_url(self.board), {'mine': '1'}).content.decode()
        self.assertIn(f'href="{board_url(self.board, tab)}?mine=1"', content)
        self.assertIn('aria-current="page"', content)
        self.assertIn('data-board-menu', content)
        self.assertIn('+ Колонка', content)
        self.assertIn(reverse('boards:sub_board_create', args=[self.board.pk, self.main.pk]), content)
        done = done_column_of(self.board)
        self.assertIn(reverse('boards:column_rename', args=[self.board.pk, self.main.pk, done.pk]), content)
        self.assertNotIn(reverse('boards:column_delete', args=[self.board.pk, self.main.pk, done.pk]), content)
        self.assertNotIn(reverse('boards:column_move', args=[self.board.pk, self.main.pk, done.pk]), content)
        self.assertIn(
            f'data-confirm-form="board-delete-column-{self.column("REVIEW").pk}"', content,
        )
        self.client.force_login(self.member)
        content = self.client.get(board_url(self.board)).content.decode()
        self.assertIn(f'href="{board_url(self.board, tab)}"', content)
        self.assertNotIn('data-board-menu', content)
        self.assertNotIn('+ Колонка', content)

    def test_the_limit_hides_the_new_column_form(self):
        for index in range(MAX_COLUMNS - 4):
            create_column(self.main, actor=self.owner, name=f'Этап {index}')
        self.client.force_login(self.owner)
        content = self.client.get(board_url(self.board)).content.decode()
        self.assertNotIn(reverse('boards:column_create', args=[self.board.pk, self.main.pk]), content)
        self.assertIn('15 колонок', content)

    def _queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(board_url(self.board), {'card': self.first.pk})
        return len(queries)

    def test_the_page_query_count_does_not_grow_with_columns_and_tabs(self):
        self.client.force_login(self.owner)
        self.first = self.card('Первая', assignees=[self.member])
        complete_task(task_of(self.card('Готовая')), self.member, 'Да')
        baseline = self._queries()
        for index in range(5):
            column = create_column(self.main, actor=self.owner, name=f'Этап {index}')
            new_card(self.board, self.member, f'Карточка {index}', assignees=[self.member], column=column)
            create_sub_board(self.board, actor=self.owner, name=f'Вкладка {index}')
        self.assertEqual(self._queries(), baseline)

    def test_cancelled_tasks_stay_off_every_column(self):
        card = self.card('Отменённая')
        Task.objects.filter(pk=task_of(card).pk).update(status=TaskStatus.objects.get(code='CANCELLED'))
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(board_url(self.board)), 'Отменённая')
