"""Recovery: revision tokens and the `/realtime/sync/` endpoint."""

from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import UserProfile
from acts.models import Act, ActComment, ActCorrectiveAction, ActHistoryEvent, ActRootAnalysis
from acts.services import add_act_comment, add_act_history_event
from notifications.models import Notification
from references.models import TaskStatus
from realtime.sync import REVISION_KEYS, build_sync_state
from tasks.models import Task, TaskAssignee
from tasks.services import complete_task, replace_task_assignees

from .base import RealtimeFixtureMixin


class SyncStateMixin(RealtimeFixtureMixin):
    def make_notification(self, recipient, act, key):
        return Notification.objects.create(
            recipient=recipient,
            actor=self.ko_user,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Заголовок',
            message='Сообщение',
            related_act=act,
            deduplication_key=key,
        )

    def make_task(self, act, assignee, text='Мероприятие'):
        root = ActRootAnalysis.objects.create(act=act, root_cause='Причина')
        action = ActCorrectiveAction.objects.create(
            root_analysis=root,
            comment=text,
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
        )
        task = Task.objects.create(
            source_action=action,
            act=act,
            root_analysis=root,
            task_text=text,
            department=self.department,
            due_date=timezone.localdate() + timedelta(days=5),
            created_by=self.otk_user,
            status=TaskStatus.objects.get(code='IN_PROGRESS'),
        )
        TaskAssignee.objects.create(task=task, user=assignee)
        return task


class RevisionTokenTests(SyncStateMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_every_documented_revision_is_present(self):
        state = build_sync_state(self.otk_user)

        self.assertEqual(sorted(state['revisions']), sorted(REVISION_KEYS))
        self.assertEqual(state['schema_version'], 1)
        self.assertTrue(state['generated_at'])
        self.assertEqual(state['unread_notifications'], 0)

    def test_tokens_are_opaque_and_short(self):
        act = self.make_act(self.status_created)
        self.make_notification(self.otk_user, act, 'opaque')

        state = build_sync_state(self.otk_user)

        for key, token in state['revisions'].items():
            with self.subTest(revision=key):
                # A fixed-length hex digest carries no readable payload at all:
                # no number, no text, no identifier can be read back out of it.
                self.assertRegex(token, r'^[0-9a-f]{16}$')
                self.assertNotIn(act.number.lower(), token.lower())
        # Different blocks must not collapse onto one value.
        self.assertEqual(len(set(state['revisions'].values())), len(state['revisions']))

    def test_a_token_does_not_move_without_a_change(self):
        act = self.make_act(self.status_created)
        self.make_notification(self.otk_user, act, 'stable')

        first = build_sync_state(self.otk_user)['revisions']
        second = build_sync_state(self.otk_user)['revisions']

        self.assertEqual(first, second)

    def test_a_new_notification_moves_only_the_notifications_token(self):
        act = self.make_act(self.status_created)
        before = build_sync_state(self.otk_user)['revisions']

        self.make_notification(self.otk_user, act, 'fresh')
        after = build_sync_state(self.otk_user)

        self.assertNotEqual(before['notifications'], after['revisions']['notifications'])
        self.assertEqual(before['tasks'], after['revisions']['tasks'])
        self.assertEqual(after['unread_notifications'], 1)

    def test_a_new_act_moves_the_acts_token(self):
        before = build_sync_state(self.otk_user)['revisions']

        self.make_act(self.status_created)
        after = build_sync_state(self.otk_user)['revisions']

        self.assertNotEqual(before['acts'], after['acts'])

    def test_a_new_task_moves_tasks_and_activities(self):
        act = self.make_act(self.status_created)
        before = build_sync_state(self.to_user)['revisions']

        self.make_task(act, self.to_user)
        after = build_sync_state(self.to_user)['revisions']

        self.assertNotEqual(before['tasks'], after['tasks'])
        self.assertNotEqual(before['activities'], after['activities'])

    def test_completing_a_task_moves_tasks_and_activities(self):
        act = self.make_act(self.status_created)
        task = self.make_task(act, self.to_user)
        before = build_sync_state(self.to_user)['revisions']

        complete_task(task, self.to_user, 'Готово')
        after = build_sync_state(self.to_user)['revisions']

        self.assertNotEqual(before['tasks'], after['tasks'])
        self.assertNotEqual(before['activities'], after['activities'])

    def test_a_new_comment_moves_the_comments_token(self):
        act = self.make_act(self.status_created)
        before = build_sync_state(self.otk_user)['revisions']

        add_act_comment(act, self.otk_user, 'Комментарий', notify=False)
        after = build_sync_state(self.otk_user)['revisions']

        self.assertNotEqual(before['comments'], after['comments'])

    def test_a_status_change_moves_acts_and_history(self):
        act = self.make_act(self.status_created)
        before = build_sync_state(self.otk_user)['revisions']

        act.status = self.status_otk_review
        act.save(update_fields=['status', 'updated_at'])
        add_act_history_event(
            act,
            self.otk_user,
            ActHistoryEvent.EventType.SENT_TO_KO,
            'Передан дальше.',
            from_status=self.status_created,
            to_status=self.status_otk_review,
            emit_notification=False,
        )
        after = build_sync_state(self.otk_user)['revisions']

        self.assertNotEqual(before['acts'], after['acts'])
        self.assertNotEqual(before['comments'], after['comments'])

    def test_globally_readable_task_moves_registry_tokens_but_not_notifications(self):
        act = self.make_act(self.status_created)
        before = build_sync_state(self.to_user)['revisions']

        # The notification stays private, while the task is globally readable.
        self.make_notification(self.otk_user, act, 'foreign')
        self.make_task(act, self.otk_user, text='Чужое мероприятие')

        after = build_sync_state(self.to_user)['revisions']

        self.assertEqual(before['notifications'], after['notifications'])
        self.assertNotEqual(before['tasks'], after['tasks'])
        self.assertNotEqual(before['activities'], after['activities'])

    def test_the_query_count_does_not_grow_with_the_data(self):
        act = self.make_act(self.status_created)
        with self.assertNumQueries(FIXED_SYNC_QUERIES):
            build_sync_state(self.otk_user)

        for index in range(5):
            self.make_notification(self.otk_user, act, f'bulk-{index}')
            self.make_task(act, self.otk_user, text=f'Мероприятие {index}')

        with self.assertNumQueries(FIXED_SYNC_QUERIES):
            build_sync_state(self.otk_user)

    def test_the_query_budget_holds_for_every_role(self):
        # The budget is pinned for every role rather than for one lucky path.
        act = self.make_act(self.status_created)
        self.make_notification(self.otk_user, act, 'role-budget')
        self.make_task(act, self.to_user)
        manager = self.make_user('rt_budget_manager', UserProfile.Role.MANAGER)

        for user in (self.otk_user, self.ko_user, self.to_user, manager):
            with self.subTest(user=user.username):
                # Warm the profile cache: `has_full_act_access` resolves
                # `user.userprofile` once per request, which is session/auth
                # work rather than part of the sync budget itself.
                build_sync_state(user)
                with self.assertNumQueries(FIXED_SYNC_QUERIES):
                    build_sync_state(user)

    def test_a_much_larger_dataset_costs_exactly_the_same_number_of_queries(self):
        act = self.make_act(self.status_created)
        build_sync_state(self.otk_user)
        with self.assertNumQueries(FIXED_SYNC_QUERIES):
            small = build_sync_state(self.otk_user)

        for index in range(25):
            self.make_notification(self.otk_user, act, f'large-{index}')
            self.make_task(act, self.otk_user, text=f'Мероприятие {index}')
            add_act_comment(act, self.otk_user, f'Комментарий {index}', notify=False)
        for index in range(10):
            self.make_act(self.status_created)

        with self.assertNumQueries(FIXED_SYNC_QUERIES):
            large = build_sync_state(self.otk_user)

        # Same cost, genuinely different state.
        self.assertNotEqual(small['revisions'], large['revisions'])

    def test_replacing_an_assignee_moves_tasks_and_activities(self):
        act = self.make_act(self.status_created)
        task = self.make_task(act, self.to_user)
        before = build_sync_state(self.to_user)['revisions']

        # Same number of assignees, different person: the count alone could
        # not tell these apart, which is what the assignment fingerprint is for.
        replace_task_assignees(task, [self.otk_user], actor=self.otk_user)

        after = build_sync_state(self.otk_user)['revisions']
        self.assertNotEqual(before['tasks'], after['tasks'])
        self.assertNotEqual(before['activities'], after['activities'])


# The service issues a fixed set of aggregate queries; it never loads rows and
# never materialises identifiers, so this number is independent of how much
# data the user can see. The nine are, in order:
#   1. notifications: total + filtered unread + max(created_at) + max(read_at)
#   2. tasks: totals, timestamps and the assignment fingerprint
#   3. tasks: status distribution
#   4. acts: active/archived totals and timestamps as filtered aggregates
#   5. acts: active status distribution
#   6. comments: count + max(created_at) over the visible-acts subquery
#   7. history: count + max(created_at) over the visible-acts subquery
#   8. activities: linked-task totals, timestamps and assignment fingerprint
#   9. activities: linked-task status distribution
#  10. workup: journal totals, confirmed count and max(updated_at)
#  11. protocols: totals, timestamps and the revision sum
#  12. protocols: status distribution
#  13. protocols: approval totals, pending count and decision timestamps
# Session authentication and the one cached `user.userprofile` lookup are not
# counted here — they belong to the request, not to this service.
FIXED_SYNC_QUERIES = 13


class SyncEndpointTests(SyncStateMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def setUp(self):
        self.url = reverse('realtime:sync')
        self.client.force_login(self.otk_user)

    def test_authentication_is_required(self):
        self.client.logout()

        response = self.client.get(self.url)

        # A technical endpoint answers 401 JSON, never an HTML login
        # redirect the fetch()-based client cannot parse as JSON.
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('Location', response)
        self.assertEqual(response.json(), {'error': 'authentication_required'})
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])

    def test_only_get_is_allowed(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)

    def test_the_payload_has_the_documented_shape(self):
        payload = self.client.get(self.url).json()

        self.assertEqual(sorted(payload), ['generated_at', 'revisions', 'schema_version', 'unread_notifications'])
        self.assertEqual(sorted(payload['revisions']), sorted(REVISION_KEYS))

    def test_no_user_parameter_is_accepted(self):
        act = self.make_act(self.status_created)
        self.make_notification(self.ko_user, act, 'theirs')

        mine = self.client.get(self.url).json()
        spoofed = self.client.get(
            self.url, {'user_id': self.ko_user.pk, 'user': self.ko_user.pk}
        ).json()

        self.assertEqual(mine['revisions'], spoofed['revisions'])
        self.assertEqual(spoofed['unread_notifications'], 0)

    def test_a_get_changes_nothing(self):
        act = self.make_act(self.status_created)
        notification = self.make_notification(self.otk_user, act, 'untouched')

        self.client.get(self.url)

        notification.refresh_from_db()
        self.assertFalse(notification.is_read)
        self.assertEqual(Act.objects.count(), 1)
        self.assertEqual(ActComment.objects.count(), 0)

    def test_the_response_is_not_cacheable(self):
        response = self.client.get(self.url)

        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('private', response['Cache-Control'])
        self.assertEqual(response['Vary'], 'Cookie')

    def test_the_response_reveals_no_transport_configuration(self):
        body = self.client.get(self.url).content.decode()

        self.assertNotIn('redis', body.lower())
        self.assertNotIn('quality-ecosystem:realtime', body)
        self.assertNotIn('channel', body.lower())

    def test_the_token_moves_after_a_visible_change(self):
        before = self.client.get(self.url).json()['revisions']

        act = self.make_act(self.status_created)
        self.make_notification(self.otk_user, act, 'moved')

        after = self.client.get(self.url).json()['revisions']

        self.assertNotEqual(before['notifications'], after['notifications'])
