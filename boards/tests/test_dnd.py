"""Dragging: the JSON contract of `card_move` and the markup the script reads."""

import json
import re

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from references.models import TaskStatus
from tasks.models import Task
from tasks.services import complete_task

from ..models import BoardCard
from ..services import create_board, create_card
from .helpers import BoardFixtureMixin, due

FETCH = {'HTTP_X_REQUESTED_WITH': 'fetch'}


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def order(board, stage):
    return list(
        BoardCard.objects.filter(board=board, stage=stage)
        .order_by('position', 'pk').values_list('title', flat=True)
    )


def tile(content, card):
    """The `<li>` of one card, from its opening tag to its end."""
    match = re.search(rf'<li class="board-column__item" data-card-id="{card.pk}".*?</li>', content, re.S)
    return match.group(0) if match else ''


class FetchMoveTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.member)
        self.a, self.b = self.card('A'), self.card('B')
        self.c = self.card('C', stage=BoardCard.Stage.REVIEW)

    def url(self, card):
        return reverse('boards:card_move', args=[self.board.pk, card.pk])

    def post(self, card, **data):
        return self.client.post(self.url(card), data, **FETCH)

    def test_before_card_puts_it_in_place(self):
        response = self.post(self.c, stage='TODO', before_card_id=self.b.pk)
        self.assertEqual(response.status_code, 200)
        answer = response.json()
        self.assertEqual(answer, {
            'ok': True,
            'stage': 'TODO',
            'counts': {'TODO': 3, 'IN_PROGRESS': 0, 'REVIEW': 0, 'DONE': 0},
        })
        self.assertEqual(order(self.board, 'TODO'), ['A', 'C', 'B'])

    def test_empty_before_card_means_the_end(self):
        response = self.post(self.a, stage='TODO', before_card_id='')
        self.assertTrue(response.json()['ok'])
        self.assertEqual(order(self.board, 'TODO'), ['B', 'A'])

    def test_foreign_before_card_is_refused(self):
        other = create_board(
            name='Другая', department=self.department, owner=self.owner, actor=self.owner,
            member_ids=[self.member.pk],
        )
        foreign = create_card(
            other, actor=self.member, title='Чужая', due_date=due(), assignee_ids=[self.member.pk],
        )
        response = self.post(self.a, stage='TODO', before_card_id=foreign.pk)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])
        self.assertIn('не найдена', response.json()['error'])

    def test_closed_task_is_400_with_the_reason(self):
        complete_task(task_of(self.a), self.member, 'Готово')
        response = self.post(self.a, stage='REVIEW')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['ok'], False)
        self.assertIn('Задача карточки закрыта', response.json()['error'])

    def test_invalid_form_is_400(self):
        response = self.post(self.a, stage='DONE')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['ok'])

    def test_no_right_is_403_json(self):
        self.client.force_login(self.outsider)
        response = self.post(self.a, stage='REVIEW')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['ok'], False)
        self.assertEqual(order(self.board, 'REVIEW'), ['C'])

    def test_ordinary_post_still_redirects(self):
        response = self.client.post(self.url(self.a), {'stage': 'REVIEW', 'before_card_id': self.c.pk})
        self.assertRedirects(
            response, f"{reverse('boards:detail', args=[self.board.pk])}?card={self.a.pk}",
        )
        self.assertEqual(order(self.board, 'REVIEW'), ['A', 'C'])

    def test_json_carries_no_markup_and_no_card_text(self):
        for response in (
            self.post(self.a, stage='REVIEW'),
            self.post(self.a, stage='DONE'),
        ):
            body = response.content.decode()
            json.loads(body)
            self.assertNotIn('<', body)
            for title in ('A', 'B', 'C'):
                self.assertNotIn(f'"{title}"', body)


class CompleteFromModalTests(BoardFixtureMixin, TestCase):
    def test_execution_comment_from_the_modal_completes(self):
        card = self.card('Карточка')
        self.client.force_login(self.member)
        response = self.client.post(
            reverse('boards:card_complete', args=[self.board.pk, card.pk]),
            {'execution_comment': 'Сделано из окна'},
        )
        self.assertRedirects(response, f"{reverse('boards:detail', args=[self.board.pk])}?card={card.pk}")
        task = task_of(card)
        self.assertEqual(task.status.code, 'COMPLETED')
        self.assertEqual(task.execution_comment, 'Сделано из окна')


class DragMarkupTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.mine = self.card('Моя', assignees=[self.member])
        self.theirs = self.card('Чужая', assignees=[self.colleague])
        self.done = self.card('Готовая', assignees=[self.member])
        complete_task(task_of(self.done), self.member, 'Да')

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(reverse('boards:detail', args=[self.board.pk])).content.decode()

    def test_reader_gets_nothing_to_drag(self):
        content = self.page(self.outsider)
        self.assertNotIn('data-card-movable', content)
        self.assertNotIn('data-card-complete-trigger', content)
        self.assertNotIn('data-column-move', content)

    def test_member_drags_and_completes_only_their_own(self):
        content = self.page(self.member)
        self.assertIn('data-card-movable', tile(content, self.mine))
        self.assertIn('data-card-movable', tile(content, self.theirs))
        self.assertIn('data-card-complete-trigger', tile(content, self.mine))
        self.assertNotIn('data-card-complete-trigger', tile(content, self.theirs))
        mine = tile(content, self.mine)
        self.assertIn(f'data-confirm-url="{reverse("boards:card_complete", args=[self.board.pk, self.mine.pk])}"', mine)
        self.assertIn('data-confirm-comment="required"', mine)
        self.assertIn('data-confirm-comment-name="execution_comment"', mine)
        self.assertIn(f'Завершить задачу №{task_of(self.mine).pk}?', mine)
        self.assertEqual(content.count('data-column-move'), 3)
        self.assertIn('data-column="DONE" data-column-complete', content)
        self.assertIn('data-board-message', content)

    def test_closed_card_is_not_movable(self):
        content = self.page(self.member)
        done = tile(content, self.done)
        self.assertTrue(done)
        self.assertNotIn('data-card-movable', done)
        self.assertNotIn('data-card-complete-trigger', done)

    def test_admin_completes_any_open_card(self):
        content = self.page(self.admin)
        self.assertIn('data-card-complete-trigger', tile(content, self.theirs))

    def test_cancelled_card_is_not_on_the_board(self):
        Task.objects.filter(pk=task_of(self.mine).pk).update(
            status=TaskStatus.objects.get(code='CANCELLED'),
        )
        self.assertEqual(tile(self.page(self.member), self.mine), '')

    def _queries(self):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(reverse('boards:detail', args=[self.board.pk]))
        return len(queries)

    def test_query_count_is_constant(self):
        self.client.force_login(self.member)
        baseline = self._queries()
        for index in range(6):
            self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
        self.assertEqual(self._queries(), baseline)
