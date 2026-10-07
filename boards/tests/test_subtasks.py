"""«Подзадачи»: cards inside a card — the model, the services, the parent's
journal, the notifications, where subtasks are and where they are not, the
panel, the table and its Excel, the live blocks and the number of queries."""

from datetime import timedelta
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from dashboard.search import quick_search
from notifications.models import Notification
from notifications.services import EMAIL_ELIGIBLE_EVENTS
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task
from tasks.presentation import describe_task_source

from ..models import MAX_SUBTASKS, BoardCard, BoardCardChecklistItem, BoardCardEvent, SubBoard
from ..selectors import build_board_state, column_counts, parse_board_filters
from ..services import (
    BoardError,
    add_checklist_item,
    archive_board,
    cancel_card,
    checklist_item_to_subtask,
    complete_card,
    create_sub_board,
    create_subtask,
    create_subtasks_from_list,
    move_card,
    remove_board_member,
    reopen_card,
    update_card,
)
from .helpers import (
    LIVE_BLOCKS,
    BoardFixtureMixin,
    assert_page_matches_fragment,
    board_url,
    column_of,
    due,
    expected_counts,
    fragment_url,
    reason_id,
)
from .test_journal import task_of
from .test_table import read_xlsx


SUBTASK_BLOCKS = {**LIVE_BLOCKS, 'subtasks': ('subtasks_html', 'subtask_summary_html', 'subtask_warning_html')}


def subtask_entries(card):
    return list(
        BoardCardEvent.objects.filter(card=card, kind=BoardCardEvent.Kind.SUBTASK)
        .order_by('pk').values_list('details', flat=True)
    )


def done_notifications():
    return Notification.objects.filter(event_type=Notification.EventType.BOARD_SUBTASKS_DONE)


class SubtaskMixin(BoardFixtureMixin):
    def setUp(self):
        self.parent = self.card(
            'Заказ 3-1579', assignees=[self.member, self.colleague], due_date=due(10),
        )

    def sub(self, title='Корпус', *, actor=None, **extra):
        return create_subtask(self.parent, actor=actor or self.member, title=title, **extra)


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------


class SubtaskModelTests(SubtaskMixin, TestCase):
    def test_one_level_only(self):
        child = self.sub()
        grandchild = BoardCard(
            board=self.board, sub_board=self.main, parent=child, position=1, number=99,
            title='Внук', created_by=self.member,
        )
        with self.assertRaisesMessage(ValidationError, 'У подзадачи не бывает подзадач'):
            grandchild.clean()
        with self.assertRaisesMessage(BoardError, 'У подзадачи не бывает подзадач'):
            create_subtask(child, actor=self.member, title='Внук')
        # A card with subtasks does not become a subtask itself.
        other = self.card('Другая')
        self.parent.parent = other
        with self.assertRaisesMessage(ValidationError, 'Карточка с подзадачами'):
            self.parent.clean()
        self.parent.parent = self.parent
        with self.assertRaisesMessage(ValidationError, 'самой себя'):
            self.parent.clean()

    def test_a_subtask_stands_in_no_column(self):
        child = self.sub()
        self.assertIsNone(child.column_id)
        self.assertEqual(child.sub_board_id, self.parent.sub_board_id)
        child.column = self.column()
        with self.assertRaises(ValidationError):
            child.clean()
        with self.assertRaises(IntegrityError), transaction.atomic():
            BoardCard.objects.filter(pk=child.pk).update(column=self.column())

    def test_the_limit(self):
        create_subtasks_from_list(self.parent, actor=self.member, text='\n'.join(f'Позиция {n}' for n in range(MAX_SUBTASKS)))
        self.assertEqual(self.parent.subtasks.count(), MAX_SUBTASKS)
        with self.assertRaisesMessage(BoardError, f'не больше {MAX_SUBTASKS} подзадач'):
            self.sub('Лишняя')
        self.assertEqual(self.parent.subtasks.count(), MAX_SUBTASKS)

    def test_the_number_is_the_boards_one_series(self):
        child = self.sub()
        later = self.card('Следующая')
        self.assertEqual(child.number, self.parent.number + 1)
        self.assertEqual(later.number, child.number + 1)
        self.assertEqual(child.code, f'{self.board.code}-{child.number}')
        self.assertEqual(child.parent_code, self.parent.code)


# --------------------------------------------------------------------------
# The services
# --------------------------------------------------------------------------


class CreateSubtaskTests(SubtaskMixin, TestCase):
    def test_the_card_gives_its_people_and_its_due_date(self):
        child = self.sub()
        task = task_of(child)
        self.assertEqual(task.source_type, Task.SourceType.BOARD)
        self.assertEqual(
            sorted(task.assignees.values_list('user_id', flat=True)), sorted([self.member.pk, self.colleague.pk]),
        )
        self.assertEqual(task.due_date, task_of(self.parent).due_date)
        self.assertEqual(task.task_text, 'Корпус')
        self.assertEqual(child.created_by, self.member)
        # What the form sends instead is taken — a later срок included.
        other = self.sub('Крышка', assignees=[self.colleague], due_date=due(30))
        self.assertEqual(list(task_of(other).assignees.values_list('user_id', flat=True)), [self.colleague.pk])
        self.assertEqual(task_of(other).due_date, due(30))
        with self.assertRaisesMessage(BoardError, 'хотя бы одного исполнителя'):
            self.sub('Без людей', assignees=[])
        with self.assertRaisesMessage(BoardError, 'активные участники'):
            self.sub('Чужой', assignees=[self.outsider])

    def test_who_may_add_one(self):
        self.sub('От администратора', actor=self.admin)
        with self.assertRaisesMessage(BoardError, 'недоступна'):
            self.sub('Чужая', actor=self.outsider)
        self.assertEqual(self.parent.subtasks.count(), 1)

    def test_only_to_an_open_card(self):
        complete_card(self.parent, actor=self.member, execution_comment='Готово')
        with self.assertRaisesMessage(BoardError, 'Карточка закрыта'):
            self.sub()
        cancelled = self.card('Отменённая')
        cancel_card(cancelled, actor=self.member, reason='Ошибка')
        with self.assertRaisesMessage(BoardError, 'Карточка закрыта'):
            create_subtask(cancelled, actor=self.member, title='Позиция')

    def test_never_on_an_archived_board(self):
        complete_card(self.parent, actor=self.member, execution_comment='Готово')
        archive_board(self.board, actor=self.owner)
        with self.assertRaisesMessage(BoardError, 'Доска в архиве'):
            self.sub()

    def test_journal_events_and_notification(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                child = self.sub('Корпус', assignees=[self.colleague])
            events = publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)
        self.assertEqual([(e.data['change'], e.data['card_id']) for e in events], [('card_created', child.pk)])
        created = child.events.get()
        self.assertEqual(created.kind, BoardCardEvent.Kind.CREATED)
        self.assertEqual(created.details, {'parent_id': self.parent.pk, 'parent': self.parent.code})
        self.assertEqual(
            subtask_entries(self.parent), [{'action': 'added', 'subtask_id': child.pk, 'code': child.code}],
        )
        notification = Notification.objects.get(
            event_type=Notification.EventType.BOARD_TASK_ASSIGNED, recipient=self.colleague,
            related_task=task_of(child),
        )
        self.assertEqual(
            notification.title,
            f'Назначена подзадача {child.code} карточки {self.parent.code} на доске «{self.board.name}»',
        )
        self.assertIn(Notification.EventType.BOARD_TASK_ASSIGNED, EMAIL_ELIGIBLE_EVENTS)


class SubtaskListTests(SubtaskMixin, TestCase):
    def test_one_line_one_subtask(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                subtasks = create_subtasks_from_list(
                    self.parent, actor=self.member, text='  Корпус \n\n Крышка\r\n   \nКрепёж',
                )
            events = publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)
        self.assertEqual([child.title for child in subtasks], ['Корпус', 'Крышка', 'Крепёж'])
        self.assertEqual([child.position for child in subtasks], [1, 2, 3])
        self.assertEqual(len(events), 1, 'one event for the whole list')
        for child in subtasks:
            self.assertEqual(task_of(child).due_date, task_of(self.parent).due_date)
        self.assertEqual(len(subtask_entries(self.parent)), 3)

    def test_all_or_nothing(self):
        for text in ('', '  \n \n', 'Хорошая\n' + 'Д' * 201):
            with self.subTest(text=text[:20]), self.assertRaises(BoardError):
                create_subtasks_from_list(self.parent, actor=self.member, text=text)
        self.sub('Первая')
        with mock.patch('boards.services.MAX_SUBTASKS', 3):
            with self.assertRaisesMessage(BoardError, 'сейчас 1, добавляется 3'):
                create_subtasks_from_list(self.parent, actor=self.member, text='А\nБ\nВ')
            self.assertEqual(self.parent.subtasks.count(), 1)
            create_subtasks_from_list(self.parent, actor=self.member, text='А\nБ')
        self.assertEqual(self.parent.subtasks.count(), 3)


class ChecklistToSubtaskTests(SubtaskMixin, TestCase):
    def test_the_item_becomes_a_subtask(self):
        first = add_checklist_item(self.parent, actor=self.member, text='Согласовать чертёж')
        second = add_checklist_item(self.parent, actor=self.member, text='Закупить металл')
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                child = checklist_item_to_subtask(second, actor=self.colleague)
            self.assertEqual(len(publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)), 1)
        self.assertEqual(child.title, 'Закупить металл')
        self.assertEqual(child.parent_id, self.parent.pk)
        self.assertFalse(BoardCardChecklistItem.objects.filter(pk=second.pk).exists())
        self.assertEqual(list(self.parent.checklist.values_list('pk', 'position')), [(first.pk, 1)])
        entry = BoardCardEvent.objects.filter(card=self.parent, kind=BoardCardEvent.Kind.CHECKLIST).last()
        self.assertEqual(entry.details['action'], 'to_subtask')
        self.assertEqual(entry.details['code'], child.code)
        self.assertNotIn('металл', str(entry.details))
        self.assertEqual(subtask_entries(self.parent)[-1]['action'], 'added')
        log = self.client_log()
        self.assertIn(f'Чек-лист: пункт стал подзадачей {child.code} (0/1)', log)
        self.assertIn(f'Подзадача {child.code} добавлена', log)

    def client_log(self):
        self.client.force_login(self.member)
        return self.client.get(fragment_url(self.board), {'card': self.parent.pk, 'tab': 'log'}).json()['log_html']

    def test_refused_for_a_subtask_and_for_a_reader(self):
        child = self.sub()
        item = add_checklist_item(child, actor=self.member, text='Шаг')
        with self.assertRaisesMessage(BoardError, 'У подзадачи не бывает подзадач'):
            checklist_item_to_subtask(item, actor=self.member)
        mine = add_checklist_item(self.parent, actor=self.member, text='Шаг')
        with self.assertRaises(BoardError):
            checklist_item_to_subtask(mine, actor=self.outsider)
        self.assertTrue(BoardCardChecklistItem.objects.filter(pk=mine.pk).exists())

    def test_the_route(self):
        item = add_checklist_item(self.parent, actor=self.member, text='Крепёж')
        url = reverse('boards:checklist_to_subtask', args=[self.board.pk, self.parent.pk, item.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_login(self.member)
        self.client.get(url)
        self.assertTrue(BoardCardChecklistItem.objects.filter(pk=item.pk).exists())
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.parent.subtasks.get().title, 'Крепёж')
        # The tool is drawn for whoever may use it, on a card, never on a subtask.
        html = self.client.get(fragment_url(self.board), {'card': self.parent.pk}).json()['checklist_html']
        self.assertNotIn('В подзадачу', html)
        add_checklist_item(self.parent, actor=self.member, text='Ещё')
        html = self.client.get(fragment_url(self.board), {'card': self.parent.pk}).json()['checklist_html']
        self.assertIn('/to-subtask/', html)
        child = self.parent.subtasks.get()
        add_checklist_item(child, actor=self.member, text='Шаг подзадачи')
        html = self.client.get(fragment_url(self.board), {'card': child.pk}).json()['checklist_html']
        self.assertNotIn('/to-subtask/', html)


class SubtaskLifeTests(SubtaskMixin, TestCase):
    def test_the_card_services_work_for_a_subtask(self):
        child = self.sub('Корпус', assignees=[self.colleague])
        update_card(
            child, actor=self.member, title='Корпус 1200', description='Сталь', due_date=due(3),
            assignee_ids=[self.colleague.pk], due_reason_id=reason_id(),
        )
        child.refresh_from_db()
        self.assertEqual((child.title, child.version), ('Корпус 1200', 2))
        complete_card(child, actor=self.colleague, execution_comment='Сварен')
        reopen_card(child, actor=self.admin)
        cancel_card(child, actor=self.member, reason='Не нужен')
        self.assertEqual(
            [entry['action'] for entry in subtask_entries(self.parent)],
            ['added', 'completed', 'reopened', 'cancelled'],
        )
        self.assertTrue(all(entry['code'] == child.code for entry in subtask_entries(self.parent)))
        self.assertEqual(
            list(child.events.order_by('pk').values_list('kind', flat=True)),
            ['CREATED', 'EDITED', 'COMPLETED', 'REOPENED', 'CANCELLED'],
        )
        # A subtask returns to its card's list, not to a column.
        self.assertEqual(child.events.get(kind='REOPENED').details, {})

    def test_a_subtask_is_not_moved_across_columns(self):
        child = self.sub()
        with self.assertRaisesMessage(BoardError, 'Подзадачу не переносят по колонкам'):
            move_card(child, actor=self.member, column=self.column('IN_PROGRESS'))
        child.refresh_from_db()
        self.assertIsNone(child.column_id)

    def test_moving_the_card_takes_its_subtasks(self):
        first, second = self.sub('Корпус'), self.sub('Крышка')
        other = create_sub_board(self.board, actor=self.owner, name='Цех ПиР')
        move_card(self.parent, actor=self.member, column=column_of(self.board, 'TODO', other))
        for child in (first, second):
            child.refresh_from_db()
            self.assertEqual(child.sub_board_id, other.pk)
            self.assertIsNone(child.column_id)
        self.assertEqual([first.position, second.position], [1, 2])

    def test_closing_the_card_with_open_subtasks_is_allowed(self):
        child = self.sub()
        complete_card(self.parent, actor=self.member, execution_comment='Отгружено')
        self.assertEqual(task_of(child).status.code, 'IN_PROGRESS')
        other = self.card('Ещё заказ')
        open_child = create_subtask(other, actor=self.member, title='Позиция')
        cancel_card(other, actor=self.member, reason='Отменён клиентом')
        self.assertEqual(task_of(open_child).status.code, 'IN_PROGRESS')

    def test_the_archive_counts_open_subtasks(self):
        child = self.sub()
        complete_card(self.parent, actor=self.member, execution_comment='Готово')
        with self.assertRaisesMessage(BoardError, 'открытые карточки: 1'):
            archive_board(self.board, actor=self.owner)
        complete_card(child, actor=self.member, execution_comment='Готово')
        archive_board(self.board, actor=self.owner)

    def test_an_assignee_of_an_open_subtask_stays_a_member(self):
        self.parent = self.card('Только мой', assignees=[self.member])
        self.sub(assignees=[self.colleague])
        with self.assertRaisesMessage(BoardError, 'исполнитель открытой карточки'):
            remove_board_member(self.board, self.colleague, actor=self.owner)


# --------------------------------------------------------------------------
# «Все подзадачи выполнены»
# --------------------------------------------------------------------------


class SubtasksDoneTests(SubtaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.first = self.sub('Корпус', assignees=[self.colleague])
        self.second = self.sub('Крышка', assignees=[self.colleague])

    def test_exactly_once_on_the_last(self):
        complete_card(self.first, actor=self.colleague, execution_comment='Готово')
        self.assertFalse(done_notifications().exists())
        complete_card(self.second, actor=self.colleague, execution_comment='Готово')
        notes = done_notifications()
        # The card's author and исполнители, except whoever closed the last one.
        self.assertEqual(sorted(note.recipient_id for note in notes), [self.member.pk])
        note = notes.get()
        self.assertEqual(note.related_task_id, task_of(self.parent).pk)
        self.assertEqual(note.title, f'Все подзадачи карточки {self.parent.code} выполнены')
        self.assertNotIn(Notification.EventType.BOARD_SUBTASKS_DONE, EMAIL_ELIGIBLE_EVENTS)
        self.assertFalse(note.deliveries.exists())
        # The card is still in work: a question, not a completion.
        self.assertEqual(task_of(self.parent).status.code, 'IN_PROGRESS')

    def test_again_after_a_reopening_and_a_new_closing(self):
        complete_card(self.first, actor=self.colleague, execution_comment='Готово')
        complete_card(self.second, actor=self.colleague, execution_comment='Готово')
        reopen_card(self.second, actor=self.admin)
        self.assertEqual(done_notifications().count(), 1)
        complete_card(self.second, actor=self.colleague, execution_comment='Переделано')
        self.assertEqual(done_notifications().count(), 2)

    def test_none_while_the_card_is_closed(self):
        complete_card(self.parent, actor=self.member, execution_comment='Отгружено')
        complete_card(self.first, actor=self.colleague, execution_comment='Готово')
        complete_card(self.second, actor=self.colleague, execution_comment='Готово')
        self.assertFalse(done_notifications().exists())

    def test_a_cancelled_last_one_asks_too_but_never_without_a_completed_one(self):
        cancel_card(self.first, actor=self.member, reason='Не нужна')
        self.assertFalse(done_notifications().exists())
        complete_card(self.second, actor=self.colleague, execution_comment='Готово')
        self.assertEqual(done_notifications().count(), 1)
        other = self.card('Пустой заказ')
        lonely = create_subtask(other, actor=self.member, title='Позиция')
        cancel_card(lonely, actor=self.member, reason='Не нужна')
        self.assertEqual(done_notifications().count(), 1)


# --------------------------------------------------------------------------
# Where subtasks are not: columns, counts, filters, «Застой»
# --------------------------------------------------------------------------


class NotOnTheBoardTests(SubtaskMixin, TestCase):
    def test_no_tile_no_count(self):
        child = self.sub('Корпус', assignees=[self.colleague])
        complete_card(self.sub('Крышка'), actor=self.member, execution_comment='Готово')
        state = build_board_state(self.board, self.main, self.member)
        tiles = [item['card'].pk for column in state['columns'] for item in column['cards']]
        self.assertEqual(tiles, [self.parent.pk])
        self.assertEqual(column_counts(self.main, self.member), expected_counts(self.board, TODO=1))
        tile = state['columns'][0]['cards'][0]
        self.assertEqual((tile['subtask_done'], tile['subtask_total']), (1, 2))
        # A card created after it still goes to the end of its column, and a
        # column holding only subtasks… holds none: it is deleted freely.
        later = self.card('Следующая')
        self.assertGreater(later.position, self.parent.position)
        self.assertNotEqual(child.position, later.position)

    def test_filters_and_stale_work_on_tiles(self):
        self.parent = self.card('Чужой заказ', assignees=[self.colleague])
        self.sub('Моя позиция', assignees=[self.member])
        column = self.column()
        column.stale_after_days = 1
        column.save()
        old = timezone_now() - timedelta(days=5)
        BoardCardEvent.objects.filter(card__parent=self.parent).update(created_at=old)
        for params in ({'mine': '1'}, {'q': 'Моя позиция'}, {'stale': '1'}):
            with self.subTest(params=params):
                state = build_board_state(
                    self.board, self.main, self.member, filters=parse_board_filters(params),
                )
                titles = [item['card'].title for column in state['columns'] for item in column['cards']]
                self.assertNotIn('Моя позиция', titles)
                self.assertNotIn('Чужой заказ', titles)


def timezone_now():
    from django.utils import timezone

    return timezone.now()


# --------------------------------------------------------------------------
# Where they are: «Задачи», the searches
# --------------------------------------------------------------------------


class SubtaskInTasksTests(SubtaskMixin, TestCase):
    def test_my_tasks_and_the_source(self):
        child = self.sub('Корпус', assignees=[self.colleague])
        self.client.force_login(self.colleague)
        response = self.client.get(reverse('tasks:list'), {'tab': 'my'})
        rows = {row['task'].pk: row for row in response.context['rows']}
        self.assertIn(task_of(child).pk, rows)
        source = describe_task_source(task_of(child))
        self.assertEqual(source['label'], f'Доска «{self.board.name}» · {child.code} · подзадача {self.parent.code}')
        self.assertIn(f'?card={child.pk}', source['url'])
        self.assertContains(response, f'подзадача {self.parent.code}')
        # The task page leads to the subtask's own drawer.
        detail = self.client.get(reverse('tasks:detail', args=[task_of(child).pk]))
        self.assertRedirects(detail, f'{board_url(self.board)}?card={child.pk}', fetch_redirect_response=False)

    def test_found_by_code_in_the_registry_and_the_topbar(self):
        child = self.sub('Корпус')
        self.client.force_login(self.member)
        response = self.client.get(reverse('tasks:list'), {'tab': 'all', 'source': child.code.lower()})
        self.assertEqual([row['task'].board_card_id for row in response.context['rows']], [child.pk])
        hits = dict(quick_search(self.member, child.code))['Задачи']
        self.assertEqual([hit.title for hit in hits], [f'Задача №{task_of(child).pk} · {child.code}'])


# --------------------------------------------------------------------------
# «Таблица» and its Excel
# --------------------------------------------------------------------------


class SubtaskTableTests(SubtaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.first = self.sub('Корпус')
        self.second = self.sub('Крышка')
        self.other = self.card('Второй заказ')
        complete_card(self.first, actor=self.member, execution_comment='Готово')
        self.client.force_login(self.member)

    def table(self, **params):
        return self.client.get(board_url(self.board), {'view': 'table', **params})

    def test_off_by_default(self):
        rows = [row['card'].pk for row in self.table().context['rows']]
        self.assertEqual(rows, [self.parent.pk, self.other.pk])
        self.assertNotContains(self.table(), 'Родитель</th>')

    def test_under_their_card(self):
        response = self.table(subtasks='1')
        rows = [(row['card'].pk, row['is_subtask']) for row in response.context['rows']]
        # The open one first, then the completed one, right under their card.
        self.assertEqual(rows, [
            (self.parent.pk, False), (self.second.pk, True), (self.first.pk, True), (self.other.pk, False),
        ])
        self.assertContains(response, '<th class="board-table__parent">Родитель</th>', html=False)
        self.assertContains(response, 'board-table__row--subtask')
        self.assertContains(response, '2 карточки и 2 подзадачи')
        # Sorting orders the cards; a card's subtasks stay under it.
        response = self.table(subtasks='1', sort='title')
        self.assertEqual(
            [row['card'].pk for row in response.context['rows']],
            [self.other.pk, self.parent.pk, self.second.pk, self.first.pk],
        )

    def test_excel(self):
        rows, _ = read_xlsx(self.table(subtasks='1', export='xlsx').content)
        header = [value for _, value in rows[0]]
        self.assertEqual(header[:3], ['Код', 'Родитель', 'Название'])
        body = [[value for _, value in row] for row in rows[1:]]
        self.assertEqual([row[0] for row in body], [self.parent.code, self.second.code, self.first.code, self.other.code])
        self.assertEqual([row[1] for row in body], [None, self.parent.code, self.parent.code, None])
        column = header.index('Колонка')
        self.assertIsNone(body[1][column])
        self.assertEqual(body[2][header.index('Статус')], 'Выполнена')
        plain, _ = read_xlsx(self.table(export='xlsx').content)
        self.assertNotIn('Родитель', [value for _, value in plain[0]])
        self.assertEqual(len(plain), 3)


# --------------------------------------------------------------------------
# The drawer
# --------------------------------------------------------------------------


class SubtaskPanelTests(SubtaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.first = self.sub('Корпус', assignees=[self.colleague])
        self.second = self.sub('Крышка', due_date=due(20))
        self.third = self.sub('Крепёж')
        complete_card(self.third, actor=self.member, execution_comment='Готово')
        self.client.force_login(self.member)

    def fragment(self, card=None, **params):
        return self.client.get(fragment_url(self.board), {'card': (card or self.parent).pk, **params}).json()

    def test_the_tabs_of_a_card_and_of_a_subtask(self):
        payload = self.fragment(tab='subtasks')
        self.assertEqual(payload['tab'], 'subtasks')
        self.assertIn('data-board-tab-link="subtasks"', payload['drawer_html'])
        self.assertIn('data-board-tab-count="subtasks">1/3<', payload['drawer_html'])
        self.assertIn('1 из 3 выполнено', payload['subtasks_html'])
        self.assertIn('Подзадачи: 1 из 3', payload['subtask_summary_html'])
        self.assertEqual(payload['subtasks_count'], '1/3')
        child = self.fragment(self.first, tab='subtasks')
        self.assertEqual(child['tab'], 'description')
        self.assertNotIn('data-board-tab-link="subtasks"', child['drawer_html'])
        self.assertEqual(child['subtasks_html'], '')
        self.assertIn('data-board-tab-link="chat"', child['drawer_html'])

    def test_a_subtask_names_its_card(self):
        child = self.fragment(self.first)
        self.assertIn(f'Подзадача карточки <a href="{board_url(self.board)}?card={self.parent.pk}&amp;tab=subtasks"', child['panel_html'])
        self.assertIn('data-board-drawer-link', child['panel_html'])
        self.assertIn(self.parent.title, child['panel_html'])
        self.assertNotIn('Переместить', child['facts_html'])
        self.assertIn('<dt>Карточка</dt>', child['facts_html'])
        late = self.fragment(self.second)
        self.assertIn('позже срока карточки', late['facts_html'])
        edit = self.fragment(self.second, edit='1')
        self.assertIn('data-subtask-due-warning', edit['card_html'])
        self.assertIn(f'Позже срока карточки {self.parent.code}', edit['card_html'])

    def test_the_rows(self):
        html = self.fragment(tab='subtasks')['subtasks_html']
        order = [html.index(child.code) for child in (self.first, self.second, self.third)]
        self.assertEqual(order, sorted(order), 'the completed one last')
        self.assertIn(f'?card={self.first.pk}', html)
        self.assertIn(f'data-task-id="{task_of(self.first).pk}"', html)
        self.assertIn('status-badge--completed', html)
        self.assertIn('позже срока карточки', html)
        self.assertIn('Скрыть выполненные', html)

    def test_hide_the_done_ones(self):
        payload = self.fragment(tab='subtasks', subtasks_done='hide')
        self.assertNotIn(self.third.code, payload['subtasks_html'])
        self.assertIn(self.first.code, payload['subtasks_html'])
        self.assertIn('Показать выполненные', payload['subtasks_html'])
        self.assertIn('subtasks_done=hide', payload['fragment_url'])
        self.assertIn('subtasks_done=hide', payload['page_url'])
        # Still «1 из 3»: hiding changes what is drawn, not what is counted.
        self.assertIn('1 из 3 выполнено', payload['subtasks_html'])

    def test_the_warnings_of_complete_cancel_and_the_drop(self):
        payload = self.fragment()
        expected = f'Открыто подзадач: 2 ({self.first.code}, {self.second.code})'
        for html in (payload['panel_html'], payload['subtask_warning_html']):
            self.assertIn('Открыто подзадач: 2 (', html)
            self.assertIn(f'?card={self.first.pk}" data-board-drawer-link>{self.first.code}</a>', html)
            self.assertIn(f'>{self.second.code}</a>', html)
        self.assertIn(expected, payload['facts_html'])  # «Отменить карточку»
        self.assertIn('data-board-cancel-trigger', payload['facts_html'])
        self.assertIn(expected, payload['columns_html'])  # the tile's drop dialog
        self.assertIn('⧉ 1/3', payload['columns_html'])
        complete_card(self.first, actor=self.colleague, execution_comment='Готово')
        complete_card(self.second, actor=self.member, execution_comment='Готово')
        payload = self.fragment()
        self.assertNotIn('Открыто подзадач', payload['panel_html'] + payload['facts_html'] + payload['columns_html'])
        self.assertIn('board-tile__subtasks--complete', payload['columns_html'])

    def test_the_page_prints_the_fragment(self):
        for params in ({'tab': 'subtasks'}, {'tab': 'subtasks', 'subtasks_done': 'hide'}, {}):
            with self.subTest(params=params):
                page = self.client.get(board_url(self.board), {'card': self.parent.pk, **params}).content.decode()
                assert_page_matches_fragment(self, page, self.fragment(**params), SUBTASK_BLOCKS)

    def test_the_forms(self):
        drawer = self.fragment(tab='subtasks')['drawer_html']
        self.assertIn(reverse('boards:subtask_create', args=[self.board.pk, self.parent.pk]), drawer)
        self.assertIn(reverse('boards:subtask_create_list', args=[self.board.pk, self.parent.pk]), drawer)
        self.assertIn(f'value="{task_of(self.parent).due_date:%Y-%m-%d}"', drawer)
        self.assertIn('Добавить списком', drawer)
        # A reader who may not add one gets the list and no form.
        self.client.force_login(self.outsider)
        drawer = self.fragment(tab='subtasks')['drawer_html']
        self.assertNotIn('/subtasks/create/', drawer)
        self.assertIn(self.first.code, drawer)


class SubtaskRouteTests(SubtaskMixin, TestCase):
    def url(self, name):
        return reverse(name, args=[self.board.pk, self.parent.pk])

    def test_create(self):
        self.client.force_login(self.member)
        url = self.url('boards:subtask_create')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.assertFalse(self.parent.subtasks.exists())
        response = self.client.post(url, {
            'subtask-title': 'Корпус', 'subtask-due_date': due(4).isoformat(),
            'subtask-assignees': [self.colleague.pk],
        })
        self.assertRedirects(
            response, f'{board_url(self.board)}?card={self.parent.pk}&tab=subtasks', fetch_redirect_response=False,
        )
        child = self.parent.subtasks.get()
        self.assertEqual(task_of(child).due_date, due(4))
        # A refusal keeps what was typed, on the tab.
        response = self.client.post(url, {
            'subtask-title': 'Д' * 150 + 'x', 'subtask-due_date': due(4).isoformat(),
            'subtask-assignees': [self.outsider.pk],
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-board-tab="subtasks"')
        self.assertContains(response, 'Д' * 150 + 'x')
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(url, {'subtask-title': 'x'}).status_code, 403)

    def test_create_from_a_list(self):
        self.client.force_login(self.member)
        url = self.url('boards:subtask_create_list')
        response = self.client.post(url, {'text': 'Корпус\nКрышка\n\nКрепёж'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(self.parent.subtasks.order_by('position').values_list('title', flat=True)),
                         ['Корпус', 'Крышка', 'Крепёж'])
        response = self.client.post(url, {'text': 'А\n' + 'Б' * 201})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Строка 2 длиннее 200 символов')
        self.assertEqual(self.parent.subtasks.count(), 3)


# --------------------------------------------------------------------------
# Live: a subtask changed by somebody else
# --------------------------------------------------------------------------


class SubtaskLiveTests(SubtaskMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.first = self.sub('Корпус', assignees=[self.colleague])
        self.second = self.sub('Крышка', assignees=[self.colleague])
        self.client.force_login(self.member)

    def fragment(self, **params):
        return self.client.get(fragment_url(self.board), {'card': self.parent.pk, **params}).json()

    def test_the_list_and_the_tile_move_the_guarded_panel_does_not(self):
        for index, params in enumerate(({}, {'edit': '1'}, {'tab': 'subtasks'})):
            with self.subTest(params=params):
                child = self.sub(f'Позиция {index}', assignees=[self.colleague])
                before = self.fragment(**params)
                complete_card(child, actor=self.colleague, execution_comment='Готово')
                after = self.fragment(**params)
                self.assertNotEqual(after['subtasks_revision'], before['subtasks_revision'])
                self.assertNotEqual(after['columns_revision'], before['columns_revision'])
                self.assertEqual(after['panel_revision'], before['panel_revision'])
                self.assertNotEqual(after['subtask_warning_html'], before['subtask_warning_html'])

    def test_one_board_event_with_the_subtasks_id(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                complete_card(self.first, actor=self.colleague, execution_comment='Готово')
            events = publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)
        self.assertEqual([(e.data['change'], e.data['card_id']) for e in events], [('card_completed', self.first.pk)])

    def test_a_task_event_of_a_subtask_is_shown_by_the_card(self):
        html = self.fragment(tab='subtasks')['subtasks_html']
        self.assertIn(f'data-task-id="{task_of(self.second).pk}"', html)


# --------------------------------------------------------------------------
# The number of queries
# --------------------------------------------------------------------------


# A sub-board page with a card open (37 before subtasks, without messages
# here) plus the card's «Подзадачи»: the subtasks with their tasks and
# statuses, and their исполнители — two queries, at 0, 1 or 20 subtasks. The
# tile's «⧉ k/n» and the codes its drop dialog names are subqueries of the
# tiles' own query; «+ Подзадача» offers the members the panel has read.
# Stage 21: one more for the card's «Переносы» (the moves of its срок).
SUBTASK_PAGE_QUERIES = 40


class SubtaskQueryCountTests(SubtaskMixin, TestCase):
    def queries(self, card=None, **params):
        self.client.force_login(self.member)
        with CaptureQueriesContext(connection) as captured:
            self.client.get(board_url(self.board), {'card': (card or self.parent).pk, **params})
        return len(captured)

    def test_constant_at_0_1_and_20(self):
        counts = {}
        counts[0] = self.queries()
        self.sub('Первая')
        counts[1] = self.queries()
        create_subtasks_from_list(self.parent, actor=self.member, text='\n'.join(f'Позиция {n}' for n in range(19)))
        complete_card(self.parent.subtasks.first(), actor=self.member, execution_comment='Готово')
        counts[20] = self.queries()
        self.assertEqual(counts, {0: SUBTASK_PAGE_QUERIES, 1: SUBTASK_PAGE_QUERIES, 20: SUBTASK_PAGE_QUERIES})
        self.assertEqual(self.queries(tab='subtasks'), SUBTASK_PAGE_QUERIES)
        # A subtask's own page reads no list, and names its card in the same query.
        self.assertLessEqual(self.queries(self.parent.subtasks.first()), SUBTASK_PAGE_QUERIES)
