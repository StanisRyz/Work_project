"""The board services: what they write, and what they refuse."""

from django.test import TestCase

from accounts.models import UserProfile
from references.models import TaskStatus
from tasks.models import Task
from tasks.services import complete_task

from ..models import Board, BoardCard, BoardMember
from ..services import (
    POSITION_STEP,
    BoardError,
    add_board_members,
    complete_card,
    compose_task_text,
    create_board,
    create_card,
    move_card,
    remove_board_member,
    update_card,
)
from .helpers import BoardFixtureMixin, done_column_of, due, fresh_code, make_user, new_card, reason_id, stage_of


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


class CreateBoardTests(BoardFixtureMixin, TestCase):
    def test_owner_is_always_a_member(self):
        board = create_board(
            code=fresh_code(),
            name='Продажи', department=self.department, owner=self.owner, actor=self.owner,
        )
        self.assertEqual(
            list(board.members.values_list('user_id', flat=True)), [self.owner.pk],
        )

    def test_a_new_board_has_one_sub_board_with_the_default_columns(self):
        board = create_board(code=fresh_code(), name='Продажи', owner=self.owner, actor=self.owner)
        sub_boards = list(board.sub_boards.all())
        self.assertEqual([(sub.name, sub.position) for sub in sub_boards], [('Основная', 1)])
        self.assertEqual(
            list(sub_boards[0].columns.order_by('position').values_list('name', 'position', 'is_done')),
            [('Сделать', 1, False), ('В работе', 2, False), ('На проверке', 3, False), ('Готово', 4, True)],
        )

    def test_refused_without_the_right(self):
        with self.assertRaises(BoardError):
            create_board(
                code=fresh_code(),
                name='Чужая', department=self.department, owner=self.member, actor=self.member,
            )
        self.assertFalse(Board.objects.filter(name='Чужая').exists())

    def test_inactive_member_is_refused(self):
        inactive = make_user('gone', UserProfile.Role.OTK)
        inactive.is_active = False
        inactive.save()
        with self.assertRaises(BoardError):
            create_board(
                code=fresh_code(),
                name='Продажи', department=self.department, owner=self.owner,
                actor=self.owner, member_ids=[inactive.pk],
            )


class MembershipTests(BoardFixtureMixin, TestCase):
    def test_owner_adds_active_users_only(self):
        added = add_board_members(self.board, [self.outsider.pk, self.member.pk], actor=self.owner)
        self.assertEqual(added, [self.outsider.pk])
        inactive = make_user('gone', UserProfile.Role.OTK)
        inactive.userprofile.is_active = False
        inactive.userprofile.save()
        with self.assertRaises(BoardError):
            add_board_members(self.board, [inactive.pk], actor=self.owner)
        self.assertFalse(BoardMember.objects.filter(board=self.board, user=inactive).exists())

    def test_member_may_not_manage(self):
        with self.assertRaises(BoardError):
            add_board_members(self.board, [self.outsider.pk], actor=self.member)

    def test_owner_cannot_be_removed(self):
        with self.assertRaises(BoardError):
            remove_board_member(self.board, self.owner, actor=self.admin)

    def test_assignee_of_an_open_card_cannot_be_removed(self):
        card = self.card(assignees=[self.colleague])
        with self.assertRaises(BoardError):
            remove_board_member(self.board, self.colleague, actor=self.owner)
        # Once the card is done the person may leave.
        complete_task(task_of(card), self.colleague, 'Готово')
        remove_board_member(self.board, self.colleague, actor=self.owner)
        self.assertFalse(BoardMember.objects.filter(board=self.board, user=self.colleague).exists())


class CreateCardTests(BoardFixtureMixin, TestCase):
    def test_creates_exactly_one_task(self):
        deadline = due(7)
        card = self.card(
            'Согласовать график', description='С цехом МП', due_date=deadline,
            assignees=[self.member, self.colleague],
        )
        tasks = Task.objects.filter(board_card=card)
        self.assertEqual(tasks.count(), 1)
        task = tasks.get()
        self.assertEqual(task.source_type, Task.SourceType.BOARD)
        self.assertEqual(task.task_text, 'Согласовать график\n\nС цехом МП')
        self.assertEqual(task.due_date, deadline)
        self.assertIsNone(task.department)
        self.assertEqual(task.status.code, 'IN_PROGRESS')
        self.assertFalse(task.requires_attachment)
        self.assertIsNone(task.individual_assignee_id)
        self.assertEqual(
            set(task.assignees.values_list('user_id', flat=True)),
            {self.member.pk, self.colleague.pk},
        )
        self.assertEqual(task.created_by, self.member)

    def test_compose_task_text(self):
        self.assertEqual(compose_task_text('  Заголовок ', ''), 'Заголовок')
        self.assertEqual(compose_task_text('Заголовок', ' Текст '), 'Заголовок\n\nТекст')

    def test_cards_go_to_the_end_of_their_column(self):
        first, second = self.card('Первая'), self.card('Вторая')
        other = self.card('Другая колонка', stage='REVIEW')
        self.assertEqual(first.position, POSITION_STEP)
        self.assertEqual(second.position, 2 * POSITION_STEP)
        self.assertEqual(other.position, POSITION_STEP)

    def test_assignee_must_be_a_member(self):
        with self.assertRaises(BoardError):
            self.card(assignees=[self.outsider])
        self.assertFalse(BoardCard.objects.exists())
        self.assertFalse(Task.objects.filter(source_type=Task.SourceType.BOARD).exists())

    def test_outsider_may_not_create(self):
        with self.assertRaises(BoardError):
            self.card(actor=self.outsider)

    def test_admin_may_create(self):
        self.card(actor=self.admin)

    def test_title_due_date_and_assignees_are_required(self):
        with self.assertRaises(BoardError):
            self.card('   ')
        with self.assertRaises(BoardError):
            self.card(due_date=None)
        with self.assertRaises(BoardError):
            self._card_without_assignees()
        with self.assertRaises(BoardError):
            self.card(column=done_column_of(self.board))
        self.assertFalse(BoardCard.objects.exists())

    def _card_without_assignees(self):
        return create_card(
            self.main, actor=self.member, title='Без исполнителей',
            due_date=due(), assignee_ids=[],
        )


class UpdateCardTests(BoardFixtureMixin, TestCase):
    def test_synchronises_the_task(self):
        card = self.card()
        deadline = due(10)
        update_card(
            card, actor=self.colleague, title='Новый заголовок', description='Подробности',
            due_date=deadline, assignee_ids=[self.colleague.pk], due_reason_id=reason_id(),
        )
        card.refresh_from_db()
        task = task_of(card)
        self.assertEqual(card.title, 'Новый заголовок')
        self.assertEqual(task.task_text, 'Новый заголовок\n\nПодробности')
        self.assertEqual(task.due_date, deadline)
        self.assertEqual(list(task.assignees.values_list('user_id', flat=True)), [self.colleague.pk])

    def test_refused_for_a_closed_task(self):
        card = self.card()
        complete_task(task_of(card), self.member, 'Сделано')
        with self.assertRaises(BoardError):
            update_card(
                card, actor=self.member, title='Позже', description='',
                due_date=due(), assignee_ids=[self.member.pk],
            )

    def test_refused_for_an_outsider_and_a_non_member_assignee(self):
        card = self.card()
        with self.assertRaises(BoardError):
            update_card(
                card, actor=self.outsider, title='X', description='',
                due_date=due(), assignee_ids=[self.member.pk],
            )
        with self.assertRaises(BoardError):
            update_card(
                card, actor=self.member, title='X', description='',
                due_date=due(), assignee_ids=[self.outsider.pk],
            )
        card.refresh_from_db()
        self.assertEqual(card.title, 'Согласовать график')


class MoveCardTests(BoardFixtureMixin, TestCase):
    def _order(self, stage):
        return list(
            BoardCard.objects.filter(column=self.column(stage))
            .order_by('position', 'pk').values_list('title', flat=True)
        )

    def test_moves_to_the_end_of_another_column(self):
        target = self.card('В работе', stage='IN_PROGRESS')
        card = self.card('Переносимая')
        move_card(card, actor=self.member, column=self.column('IN_PROGRESS'))
        self.assertEqual(stage_of(card), 'IN_PROGRESS')
        self.assertGreater(card.position, target.position)
        self.assertEqual(self._order('IN_PROGRESS'), ['В работе', 'Переносимая'])

    def test_moves_before_a_card(self):
        a, b, c = self.card('A'), self.card('B'), self.card('C')
        move_card(c, actor=self.member, column=self.column('TODO'), before_card_id=b.pk)
        self.assertEqual(self._order('TODO'), ['A', 'C', 'B'])
        move_card(b, actor=self.member, column=self.column('TODO'), before_card_id=a.pk)
        self.assertEqual(self._order('TODO'), ['B', 'A', 'C'])
        # A form posts the id as text.
        move_card(c, actor=self.member, column=self.column('TODO'), before_card_id=str(b.pk))
        self.assertEqual(self._order('TODO'), ['C', 'B', 'A'])

    def test_renumbers_when_the_gap_is_gone(self):
        a, b, c = self.card('A'), self.card('B'), self.card('C')
        BoardCard.objects.filter(pk=a.pk).update(position=10)
        BoardCard.objects.filter(pk=b.pk).update(position=11)
        move_card(c, actor=self.member, column=self.column('TODO'), before_card_id=b.pk)
        self.assertEqual(self._order('TODO'), ['A', 'C', 'B'])
        positions = list(
            BoardCard.objects.filter(column=self.column('TODO'))
            .order_by('position').values_list('position', flat=True)
        )
        self.assertEqual(positions, [POSITION_STEP, 2 * POSITION_STEP, 3 * POSITION_STEP])

    def test_refused_for_a_closed_task(self):
        card = self.card()
        complete_task(task_of(card), self.member, 'Сделано')
        with self.assertRaises(BoardError):
            move_card(card, actor=self.member, column=self.column('REVIEW'))
        self.assertEqual(stage_of(card), 'TODO')

    def test_refused_for_a_foreign_or_missing_before_card(self):
        card = self.card()
        other_board = create_board(
            code=fresh_code(),
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
            member_ids=[self.member.pk],
        )
        foreign = new_card(other_board, self.member, 'Чужая', assignees=[self.member])
        for before in (foreign.pk, 999999, 'abc', card.pk):
            with self.subTest(before=before), self.assertRaises(BoardError):
                move_card(card, actor=self.member, column=self.column('TODO'), before_card_id=before)

    def test_refused_for_done_column_and_outsider(self):
        card = self.card()
        with self.assertRaises(BoardError):
            move_card(card, actor=self.member, column=done_column_of(self.board))
        with self.assertRaises(BoardError):
            move_card(card, actor=self.outsider, column=self.column('REVIEW'))


class CompleteCardTests(BoardFixtureMixin, TestCase):
    def test_requires_a_comment(self):
        card = self.card()
        with self.assertRaises(BoardError):
            complete_card(card, actor=self.member, execution_comment='   ')
        self.assertEqual(task_of(card).status.code, 'IN_PROGRESS')

    def test_requires_the_assignee(self):
        card = self.card(assignees=[self.member])
        # A member who is not the исполнитель, and the owner, may not finish it.
        for actor in (self.colleague, self.owner, self.outsider):
            with self.subTest(actor=actor.username), self.assertRaises(BoardError):
                complete_card(card, actor=actor, execution_comment='Готово')

    def test_completes_the_task_and_keeps_the_column(self):
        card = self.card(stage='REVIEW')
        complete_card(card, actor=self.member, execution_comment='Сделано')
        task = task_of(card)
        card.refresh_from_db()
        self.assertEqual(task.status, TaskStatus.objects.get(code='COMPLETED'))
        self.assertEqual(task.execution_comment, 'Сделано')
        self.assertEqual(stage_of(card), 'REVIEW')
