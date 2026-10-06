"""The live board: `board.updated`, its audience, `boards:fragment`, the sync token."""

import json
import re

from django.contrib.auth.models import User
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from accounts.models import UserProfile
from realtime.events import (
    BOARD_CHANGES,
    RESOURCE_BOARD,
    RealtimeEventError,
    RealtimeEventType,
)
from realtime.factories import board_updated_event
from realtime.fragments import content_revision
from realtime.recipients import board_targets
from realtime.sync import REVISION_BOARDS, build_sync_state
from realtime.testing import capture_realtime_events
from realtime.tests.base import target_keys
from tasks.drafts import remember_execution_draft
from tasks.models import Task
from tasks.services import complete_task, reopen_task

from ..models import BoardCard
from ..services import (
    BoardError,
    add_board_members,
    complete_card,
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
    remove_board_member,
    update_card,
)
from .helpers import BoardFixtureMixin, board_url, due, fragment_url, make_user


def task_of(card):
    return Task.objects.get(source_type=Task.SourceType.BOARD, board_card=card)


def board_events(publisher):
    return publisher.events_of_type(RealtimeEventType.BOARD_UPDATED)


CSRF_INPUT = re.compile(r'<input\b[^>]*\bname="csrfmiddlewaretoken"[^>]*>')


def same_markup(fragment, page):
    """Is `fragment` part of `page`, CSRF tokens aside (masked anew per render)?"""
    return CSRF_INPUT.sub('', fragment) in CSRF_INPUT.sub('', page)


def attribute(content, name):
    match = re.search(rf'{name}="([^"]*)"', content)
    return match.group(1) if match else None


class BoardEventContractTests(TestCase):
    def test_type_payload_and_change_codes(self):
        self.assertEqual(RealtimeEventType.BOARD_UPDATED.value, 'board.updated')
        self.assertEqual(
            BOARD_CHANGES,
            {
                'card_created', 'card_updated', 'card_moved', 'card_completed', 'members_changed',
                'card_cancelled', 'board_archived', 'board_restored', 'comment_added',
                'structure_changed',
            },
        )
        event = board_updated_event(7, 'card_moved', 12)
        self.assertEqual(event.resource_type, RESOURCE_BOARD)
        self.assertEqual(event.resource_id, 7)
        self.assertEqual(event.data, {'board_id': 7, 'card_id': 12, 'change': 'card_moved'})
        self.assertEqual(board_updated_event(7, 'members_changed').data['card_id'], None)

    def test_an_unknown_change_is_refused(self):
        with self.assertRaises(RealtimeEventError):
            board_updated_event(7, 'card_deleted', 12)


class BoardTargetsTests(BoardFixtureMixin, TestCase):
    def test_every_active_account_and_nobody_else(self):
        inactive = make_user('rt_board_inactive')
        inactive.is_active = False
        inactive.save()
        dormant = make_user('rt_board_dormant')
        UserProfile.objects.filter(user=dormant).update(is_active=False)
        root = User.objects.create_superuser(username='rt_board_root', password='x')
        UserProfile.objects.filter(user=root).delete()

        # The board tests run with full access widened to every role, so
        # every active account reads this board.
        keys = target_keys(board_targets(self.board))
        for user in (self.owner, self.member, self.colleague, self.outsider, self.admin, root):
            self.assertIn(f'user:{user.pk}', keys)
        for user in (inactive, dormant):
            self.assertNotIn(f'user:{user.pk}', keys)
        self.assertTrue(all(key.startswith('user:') for key in keys))


class BoardEmissionTests(BoardFixtureMixin, TestCase):
    """Exactly one event per successful write, after the commit, and no other."""

    def setUp(self):
        self.a = self.card('A')
        self.b = self.card('B')

    def emitted(self, action):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                action()
                self.assertEqual(board_events(publisher), [], 'опубликовано до commit')
            return board_events(publisher), publisher

    def assert_one(self, action, change, card=None):
        events, publisher = self.emitted(action)
        self.assertEqual(len(events), 1, events)
        event = events[0]
        self.assertEqual(event.resource_id, self.board.pk)
        self.assertEqual(event.data['change'], change)
        self.assertEqual(event.data['card_id'], card.pk if card else None)
        self.assertEqual(sorted(event.data), ['board_id', 'card_id', 'change'])
        body = json.dumps(event.as_dict(), ensure_ascii=False)
        for text in ('A', 'B', 'Планирование', 'member_one'):
            self.assertNotIn(f'"{text}"', body)
        self.assertNotIn('Планирование', body)
        targets = target_keys(publisher.targets_of_type(RealtimeEventType.BOARD_UPDATED)[0])
        self.assertIn(f'user:{self.outsider.pk}', targets)
        return event

    def test_create(self):
        created = []
        events, _ = self.emitted(lambda: created.append(self.card('C')))
        self.assertEqual([e.data['change'] for e in events], ['card_created'])
        self.assertEqual(events[0].data['card_id'], created[0].pk)

    def test_update(self):
        self.assert_one(
            lambda: update_card(
                self.a, actor=self.member, title='A2', description='', due_date=due(),
                assignee_ids=[self.member.pk],
            ),
            'card_updated', self.a,
        )

    def test_update_of_the_deadline_or_assignees_alone_counts(self):
        self.assert_one(
            lambda: update_card(
                self.a, actor=self.member, title='A', description='', due_date=due(9),
                assignee_ids=[self.member.pk],
            ),
            'card_updated', self.a,
        )
        self.assert_one(
            lambda: update_card(
                self.a, actor=self.member, title='A', description='', due_date=due(9),
                assignee_ids=[self.member.pk, self.colleague.pk],
            ),
            'card_updated', self.a,
        )

    def test_an_empty_edit_writes_and_emits_nothing(self):
        before = BoardCard.objects.get(pk=self.a.pk).updated_at
        task_before = task_of(self.a).updated_at
        events, publisher = self.emitted(
            lambda: update_card(
                self.a, actor=self.member, title='A', description='', due_date=due(),
                assignee_ids=[self.member.pk],
            )
        )
        self.assertEqual(events, [])
        self.assertEqual(publisher.events, [])
        self.assertEqual(BoardCard.objects.get(pk=self.a.pk).updated_at, before)
        self.assertEqual(task_of(self.a).updated_at, task_before)

    def test_move(self):
        self.assert_one(
            lambda: move_card(self.a, actor=self.member, column=self.column('REVIEW')), 'card_moved', self.a,
        )
        self.assert_one(
            lambda: move_card(self.b, actor=self.member, column=self.column('REVIEW'), before_card_id=self.a.pk),
            'card_moved', self.b,
        )

    def test_a_move_to_the_same_place_writes_and_emits_nothing(self):
        for stage, before in (('TODO', self.b.pk), ('TODO', None)):
            card = self.a if before else self.b
            stored = BoardCard.objects.get(pk=card.pk)
            events, publisher = self.emitted(
                lambda card=card, stage=stage, before=before: move_card(
                    card, actor=self.member, column=self.column(stage), before_card_id=before,
                )
            )
            self.assertEqual(events, [])
            self.assertEqual(publisher.events, [])
            fresh = BoardCard.objects.get(pk=card.pk)
            self.assertEqual((fresh.column_id, fresh.position, fresh.updated_at),
                             (stored.column_id, stored.position, stored.updated_at))

    def test_structure(self):
        sub = {}
        self.assert_one(
            lambda: sub.setdefault('tab', create_sub_board(self.board, actor=self.owner, name='Цех')),
            'structure_changed',
        )
        tab = sub['tab']
        self.assert_one(lambda: rename_sub_board(tab, actor=self.admin, name='Цех МП'), 'structure_changed')
        events, _ = self.emitted(lambda: rename_sub_board(tab, actor=self.owner, name='Цех МП'))
        self.assertEqual(events, [], 'то же название — события нет')
        self.assert_one(lambda: move_sub_board(tab, actor=self.owner, direction='left'), 'structure_changed')
        column = {}
        self.assert_one(
            lambda: column.setdefault('new', create_column(tab, actor=self.owner, name='ОТК')),
            'structure_changed',
        )
        self.assert_one(lambda: rename_column(column['new'], actor=self.owner, name='Приёмка'), 'structure_changed')
        events, _ = self.emitted(lambda: rename_column(column['new'], actor=self.owner, name=' Приёмка '))
        self.assertEqual(events, [], 'то же название после обрезки — события нет')
        self.assert_one(lambda: move_column(column['new'], actor=self.owner, direction='left'), 'structure_changed')
        self.assert_one(lambda: delete_column(column['new'], actor=self.owner), 'structure_changed')
        self.assert_one(lambda: delete_sub_board(tab, actor=self.owner), 'structure_changed')

    def test_complete(self):
        self.assert_one(
            lambda: complete_card(self.a, actor=self.member, execution_comment='Сделано'),
            'card_completed', self.a,
        )

    def test_members(self):
        newcomer = make_user('rt_board_newcomer')
        self.assert_one(
            lambda: add_board_members(self.board, [newcomer.pk], actor=self.owner),
            'members_changed',
        )
        events, _ = self.emitted(
            lambda: add_board_members(self.board, [newcomer.pk], actor=self.owner)
        )
        self.assertEqual(events, [], 'никто не добавлен — события нет')
        self.assert_one(
            lambda: remove_board_member(self.board, newcomer, actor=self.owner),
            'members_changed',
        )

    def test_a_refusal_emits_nothing(self):
        refusals = [
            lambda: create_card(
                self.main, actor=self.outsider, title='X', due_date=due(),
                assignee_ids=[self.member.pk],
            ),
            lambda: update_card(
                self.a, actor=self.outsider, title='X', description='', due_date=due(),
                assignee_ids=[self.member.pk],
            ),
            lambda: move_card(self.a, actor=self.outsider, column=self.column('REVIEW')),
            lambda: complete_card(self.a, actor=self.colleague, execution_comment='Не моя'),
            lambda: complete_card(self.a, actor=self.member, execution_comment='   '),
            lambda: remove_board_member(self.board, self.owner, actor=self.owner),
            lambda: add_board_members(self.board, [self.outsider.pk], actor=self.member),
            lambda: create_sub_board(self.board, actor=self.member, name='Нельзя'),
            lambda: create_sub_board(self.board, actor=self.owner, name='  '),
            lambda: create_sub_board(self.board, actor=self.owner, name='основная'),
            lambda: rename_sub_board(self.main, actor=self.member, name='Нельзя'),
            lambda: move_sub_board(self.main, actor=self.owner, direction='left'),
            lambda: delete_sub_board(self.main, actor=self.owner),
            lambda: create_column(self.main, actor=self.member, name='Нельзя'),
            lambda: rename_column(self.column('TODO'), actor=self.owner, name=''),
            lambda: move_column(self.column('TODO'), actor=self.owner, direction='left'),
            lambda: delete_column(self.column('TODO'), actor=self.owner),
        ]
        for index, refusal in enumerate(refusals):
            with self.subTest(refusal=index):
                with capture_realtime_events() as publisher:
                    with self.captureOnCommitCallbacks(execute=True):
                        with self.assertRaises(BoardError):
                            refusal()
                    self.assertEqual(board_events(publisher), [])

    def test_a_rollback_emits_nothing(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        move_card(self.a, actor=self.member, column=self.column('REVIEW'))
                        raise RuntimeError('rollback')
                except RuntimeError:
                    pass
            self.assertEqual(board_events(publisher), [])
        self.assertEqual(BoardCard.objects.get(pk=self.a.pk).column, self.column('TODO'))

    def test_task_changes_outside_the_board_emit_no_board_event(self):
        task = task_of(self.a)
        events, publisher = self.emitted(lambda: complete_task(task, self.member, 'Готово'))
        self.assertEqual(events, [])
        self.assertTrue(publisher.events_of_type(RealtimeEventType.TASK_COMPLETED))
        events, _ = self.emitted(lambda: reopen_task(task_of(self.a), self.admin))
        self.assertEqual(events, [])


class BoardFragmentTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.a = self.card('Альфа', assignees=[self.member])
        self.b = self.card('Бета', assignees=[self.colleague], stage='REVIEW')
        self.done = self.card('Готовая', assignees=[self.member])
        complete_task(task_of(self.done), self.member, 'Да')

    def url(self, **query):
        url = fragment_url(self.board)
        if query:
            url += '?' + '&'.join(f'{key}={value}' for key, value in query.items())
        return url

    def page(self, **query):
        url = board_url(self.board)
        if query:
            url += '?' + '&'.join(f'{key}={value}' for key, value in query.items())
        return self.client.get(url).content.decode()

    def test_anonymous_gets_a_json_401(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response['Content-Type'], 'application/json')

    def test_post_is_refused_and_nothing_cached(self):
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self.url()).status_code, 405)
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])

    def test_a_missing_board_is_404(self):
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get(reverse('boards:fragment', args=[self.board.pk + 100, self.main.pk])).status_code, 404,
        )

    def test_a_deleted_or_foreign_sub_board_is_404(self):
        self.client.force_login(self.member)
        tab = create_sub_board(self.board, actor=self.owner, name='Временная')
        url = reverse('boards:fragment', args=[self.board.pk, tab.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        delete_sub_board(tab, actor=self.owner)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {'error': 'not_found'})
        other = create_board(name='Другая', owner=self.owner, actor=self.owner, member_ids=[self.member.pk])
        foreign_tab = other.sub_boards.get()
        self.assertEqual(
            self.client.get(reverse('boards:fragment', args=[self.board.pk, foreign_tab.pk])).status_code,
            404,
        )

    def test_page_and_fragment_are_equal_for_a_second_sub_board(self):
        tab = create_sub_board(self.board, actor=self.owner, name='Вторая')
        create_column(tab, actor=self.owner, name='Приёмка')
        card = self.card('На второй', sub_board=tab, stage='IN_PROGRESS')
        for user in (self.owner, self.member, self.outsider):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                for query in ({}, {'card': card.pk}):
                    page = self.client.get(board_url(self.board, tab), query).content.decode()
                    fragment = self.client.get(fragment_url(self.board, tab), query).json()
                    self.assertTrue(same_markup(fragment['columns_html'], page))
                    self.assertEqual(attribute(page, 'data-columns-revision'), fragment['columns_revision'])
                    self.assertEqual(attribute(page, 'data-panel-revision'), fragment['panel_revision'])
                    self.assertIn('Приёмка', fragment['columns_html'])
                    self.assertIn('На второй', fragment['columns_html'])
                    self.assertNotIn('Альфа', fragment['columns_html'])

    def test_the_structure_fingerprint_moves_with_a_column(self):
        self.client.force_login(self.member)
        before = self.client.get(self.url()).json()['columns_revision']
        column = create_column(self.main, actor=self.owner, name='Приёмка')
        after = self.client.get(self.url()).json()
        self.assertNotEqual(after['columns_revision'], before)
        self.assertIn('Приёмка', after['columns_html'])
        rename_column(column, actor=self.owner, name='Контроль')
        self.assertIn('Контроль', self.client.get(self.url()).json()['columns_html'])
        tab = create_sub_board(self.board, actor=self.owner, name='Новая вкладка')
        self.assertIn('Новая вкладка', self.client.get(self.url()).json()['tabs_html'])
        delete_sub_board(tab, actor=self.owner)
        self.assertNotIn('Новая вкладка', self.client.get(self.url()).json()['tabs_html'])

    def test_a_reader_gets_what_the_page_shows(self):
        self.client.force_login(self.outsider)
        payload = self.client.get(self.url()).json()
        content = self.page()
        self.assertTrue(same_markup(payload['columns_html'], content))
        self.assertEqual(attribute(content, 'data-columns-revision'), payload['columns_revision'])
        self.assertEqual(payload['columns_revision'], content_revision(payload['columns_html']))
        self.assertEqual(payload['panel_html'], '')
        self.assertEqual(payload['panel_revision'], '')
        self.assertNotIn('data-card-movable', payload['columns_html'])

    def test_the_same_blocks_and_fingerprints_as_the_page_for_each_panel(self):
        self.client.force_login(self.member)
        for query, panel in (
            ({'card': self.a.pk}, 'view'),
            ({'card': self.a.pk, 'edit': '1'}, 'edit'),
            ({'new': self.column('REVIEW').pk}, 'new'),
            ({'card': self.done.pk}, 'view'),
        ):
            with self.subTest(query=query):
                payload = self.client.get(self.url(**query)).json()
                content = self.page(**query)
                self.assertEqual(payload['panel'], panel)
                self.assertTrue(same_markup(payload['columns_html'], content))
                self.assertTrue(same_markup(payload['panel_html'], content))
                self.assertTrue(payload['panel_html'].strip())
                self.assertEqual(attribute(content, 'data-columns-revision'), payload['columns_revision'])
                self.assertEqual(attribute(content, 'data-panel-revision'), payload['panel_revision'])
                self.assertEqual(attribute(content, 'data-panel-holds-input'), 'false')
                expected_url = self.url(**query).replace('&', '&amp;')
                self.assertEqual(attribute(content, 'data-board-fragment-url'), expected_url)

    def test_the_panel_follows_the_query(self):
        self.client.force_login(self.member)
        view = self.client.get(self.url(card=self.a.pk)).json()['panel_html']
        self.assertIn('Карточка №', view)
        self.assertIn('Выполнение', view)
        edit = self.client.get(self.url(card=self.a.pk, edit='1')).json()['panel_html']
        self.assertIn('Редактирование', edit)
        self.assertIn('value="Альфа"', edit)
        review = self.column('REVIEW')
        new = self.client.get(self.url(new=review.pk)).json()['panel_html']
        self.assertIn('Новая карточка', new)
        self.assertIn(f'name="column" value="{review.pk}"', new)
        # Not a member: no edit, no new — the same answer as the page.
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url(card=self.a.pk, edit='1')).json()['panel'], 'view')
        self.assertEqual(self.client.get(self.url(new=self.column('TODO').pk)).json()['panel'], '')

    def test_the_fragment_does_not_take_the_draft(self):
        self.client.force_login(self.member)
        session = self.client.session
        request = type('R', (), {'session': session})()
        remember_execution_draft(request, task_of(self.a), 'Черновик результата')
        session.save()
        payload = self.client.get(self.url(card=self.a.pk)).json()
        self.assertNotIn('Черновик результата', payload['panel_html'])
        content = self.page(card=self.a.pk)
        self.assertIn('Черновик результата', content)
        # The page holds unsaved text: it starts dirty, with the clean fingerprint.
        self.assertEqual(attribute(content, 'data-panel-holds-input'), 'true')
        self.assertEqual(attribute(content, 'data-panel-revision'), payload['panel_revision'])

    def test_a_refused_post_page_carries_the_clean_fingerprint_and_its_own_address(self):
        self.client.force_login(self.member)
        clean = self.client.get(self.url(card=self.a.pk, edit='1')).json()
        response = self.client.post(
            reverse('boards:card_update', args=[self.board.pk, self.a.pk]),
            {'title': '', 'due_date': due().isoformat(), 'assignees': [self.member.pk]},
        )
        content = response.content.decode()
        self.assertEqual(attribute(content, 'data-panel-holds-input'), 'true')
        self.assertEqual(attribute(content, 'data-panel-revision'), clean['panel_revision'])
        page = board_url(self.board)
        self.assertEqual(attribute(content, 'data-board-url'), page)
        self.assertEqual(
            attribute(content, 'data-board-page-url'), f'{page}?card={self.a.pk}&amp;edit=1',
        )

    def test_a_refused_completion_keeps_the_text_and_starts_dirty(self):
        self.client.force_login(self.member)
        clean = self.client.get(self.url(card=self.a.pk)).json()
        response = self.client.post(
            reverse('boards:card_complete', args=[self.board.pk, self.a.pk]),
            {'execution_comment': '   '},
        )
        content = response.content.decode()
        self.assertEqual(attribute(content, 'data-panel-holds-input'), 'true')
        self.assertEqual(attribute(content, 'data-panel-revision'), clean['panel_revision'])

    def test_the_fingerprint_moves_with_the_card(self):
        self.client.force_login(self.member)
        before = self.client.get(self.url(card=self.a.pk)).json()
        again = self.client.get(self.url(card=self.a.pk)).json()
        self.assertEqual(before['columns_revision'], again['columns_revision'])
        self.assertEqual(before['panel_revision'], again['panel_revision'])
        move_card(self.a, actor=self.member, column=self.column('REVIEW'))
        after = self.client.get(self.url(card=self.a.pk)).json()
        self.assertNotEqual(before['columns_revision'], after['columns_revision'])
        self.assertNotEqual(before['panel_revision'], after['panel_revision'])
        other = self.client.get(self.url(card=self.b.pk)).json()
        move_card(self.a, actor=self.member, column=self.column('TODO'))
        self.assertEqual(
            self.client.get(self.url(card=self.b.pk)).json()['panel_revision'],
            other['panel_revision'],
        )

    def _queries(self, **query):
        with CaptureQueriesContext(connection) as queries:
            self.client.get(self.url(**query))
        return len(queries)

    def test_query_count_is_constant(self):
        self.client.force_login(self.member)
        baseline = self._queries(card=self.a.pk)
        for index in range(5):
            card = self.card(f'Ещё {index}', assignees=[self.member, self.colleague])
            if index % 2:
                complete_task(task_of(card), self.member, 'Да')
        self.assertEqual(self._queries(card=self.a.pk), baseline)


class BoardSyncRevisionTests(BoardFixtureMixin, TestCase):
    def setUp(self):
        self.a = self.card('A')

    def token(self, user=None):
        return build_sync_state(user or self.outsider)['revisions'][REVISION_BOARDS]

    def assert_moves(self, action):
        before = self.token()
        action()
        self.assertNotEqual(self.token(), before)

    def test_the_token_is_the_same_for_every_reader(self):
        self.assertEqual(self.token(self.member), self.token(self.outsider))

    def test_it_moves_on_every_board_change(self):
        self.assert_moves(lambda: self.card('B'))
        self.assert_moves(lambda: move_card(self.a, actor=self.member, column=self.column('REVIEW')))
        self.client.force_login(self.member)
        self.assert_moves(lambda: self.client.post(
            reverse('tasks:complete', args=[task_of(self.a).pk]),
            {'execution_comment': 'Готово'},
        ))
        self.assertEqual(task_of(self.a).status.code, 'COMPLETED')
        self.assert_moves(lambda: reopen_task(task_of(self.a), self.admin))
        newcomer = make_user('rt_board_sync_newcomer')
        self.assert_moves(lambda: add_board_members(self.board, [newcomer.pk], actor=self.owner))
        self.assert_moves(lambda: remove_board_member(self.board, newcomer, actor=self.owner))

    def test_it_moves_on_every_structure_change(self):
        holder = {}
        self.assert_moves(lambda: holder.setdefault(
            'sub', create_sub_board(self.board, actor=self.owner, name='Вторая'),
        ))
        self.assert_moves(lambda: rename_sub_board(holder['sub'], actor=self.owner, name='Цех'))
        self.assert_moves(lambda: move_sub_board(holder['sub'], actor=self.owner, direction='left'))
        self.assert_moves(lambda: holder.setdefault(
            'column', create_column(self.main, actor=self.owner, name='Проверка'),
        ))
        self.assert_moves(lambda: rename_column(holder['column'], actor=self.owner, name='ОТК'))
        self.assert_moves(lambda: move_column(holder['column'], actor=self.owner, direction='left'))
        self.assert_moves(lambda: delete_column(holder['column'], actor=self.owner))
        self.assert_moves(lambda: delete_sub_board(holder['sub'], actor=self.owner))

    def test_it_stays_put_otherwise(self):
        before = self.token()
        move_card(self.a, actor=self.member, column=self.column('TODO'))
        update_card(
            self.a, actor=self.member, title='A', description='', due_date=due(),
            assignee_ids=[self.member.pk],
        )
        self.client.force_login(self.member)
        self.client.get(board_url(self.board))
        self.client.get(fragment_url(self.board))
        self.assertEqual(self.token(), before)
