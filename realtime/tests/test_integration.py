import json

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from acts.models import Act, ActDefect
from acts.services import (
    add_act_comment,
    apply_ko_decision,
    approve_act,
    return_to_otk,
    send_to_ko,
)
from notifications.models import Notification
from notifications.services import create_notifications, mark_notifications_read
from realtime.events import RealtimeEventType
from realtime.testing import capture_realtime_events
from tasks.models import Task, TaskAssignee
from tasks.services import complete_task

from .base import RealtimeFixtureMixin, target_keys


class NotificationEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_creating_an_internal_notification_publishes_one_event_per_row(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                created = create_notifications(
                    event_type=Notification.EventType.COMMENT_ADDED,
                    act=act,
                    actor=self.ko_user,
                    recipients=[self.otk_user, self.to_user],
                    source_key='integration:1',
                )

        events = publisher.events_of_type(RealtimeEventType.NOTIFICATION_CREATED)
        self.assertEqual(len(events), len(created), 'одно событие на созданную запись')
        self.assertEqual(
            sorted(event.resource_id for event in events),
            sorted(notification.pk for notification in created),
        )

    def test_a_notification_is_targeted_only_at_its_recipient(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                create_notifications(
                    event_type=Notification.EventType.COMMENT_ADDED,
                    act=act,
                    actor=self.ko_user,
                    recipients=[self.otk_user],
                    source_key='integration:2',
                )

        _event, targets = publisher.published[0]
        self.assertEqual(target_keys(targets), [f'user:{self.otk_user.pk}'])

    def test_a_deduplicated_notification_does_not_publish_a_second_event(self):
        act = self.make_act(self.status_created)
        payload = {
            'event_type': Notification.EventType.COMMENT_ADDED,
            'act': act,
            'actor': self.ko_user,
            'recipients': [self.otk_user],
            'source_key': 'integration:duplicate',
        }
        with self.captureOnCommitCallbacks(execute=True):
            create_notifications(**payload)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                created = create_notifications(**payload)

        self.assertEqual(created, [])
        self.assertEqual(publisher.published, [])

    def test_notification_payload_carries_no_text(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                created = create_notifications(
                    event_type=Notification.EventType.COMMENT_ADDED,
                    act=act,
                    actor=self.ko_user,
                    recipients=[self.otk_user],
                    source_key='integration:3',
                )

        payload = json.dumps(publisher.events[0].as_dict(), ensure_ascii=False)
        self.assertNotIn(created[0].title, payload)
        self.assertNotIn(created[0].message, payload)
        self.assertNotIn(act.number, payload)
        self.assertNotIn(self.otk_user.username, payload)


class NotificationReadEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def setUp(self):
        self.act = self.make_act(self.status_created)
        self.client.force_login(self.otk_user)

    def _make_notification(self, key):
        return Notification.objects.create(
            recipient=self.otk_user,
            actor=self.ko_user,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Заголовок',
            message='Сообщение',
            related_act=self.act,
            deduplication_key=key,
        )

    def test_marking_one_notification_publishes_one_aggregated_event(self):
        notification = self._make_notification('read:1')
        self._make_notification('read:2')

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    reverse('notifications:mark_read', args=[notification.pk])
                )

        events = publisher.events_of_type(RealtimeEventType.NOTIFICATION_READ)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_type, 'user')
        self.assertEqual(events[0].resource_id, self.otk_user.pk)
        self.assertEqual(events[0].data['notification_ids'], [notification.pk])
        self.assertEqual(events[0].data['changed_count'], 1)
        self.assertEqual(events[0].data['unread_count'], 1)
        self.assertEqual(events[0].data['scope'], 'single')

    def test_the_bell_bulk_action_publishes_one_event_for_the_user(self):
        shown = [self._make_notification(f'bell:{index}') for index in range(3)]
        self._make_notification('bell:hidden')

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    reverse('notifications:mark_read_bulk'),
                    {'ids': [item.pk for item in shown]},
                )

        events = publisher.events_of_type(RealtimeEventType.NOTIFICATION_READ)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].data['notification_ids'], sorted(item.pk for item in shown)
        )
        self.assertEqual(events[0].data['changed_count'], 3)
        self.assertEqual(events[0].data['unread_count'], 1)
        self.assertEqual(events[0].data['scope'], 'bell')

    def test_mark_all_publishes_one_event_for_the_user(self):
        for index in range(4):
            self._make_notification(f'all:{index}')

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse('notifications:mark_all_read'))

        events = publisher.events_of_type(RealtimeEventType.NOTIFICATION_READ)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data['changed_count'], 4)
        self.assertEqual(events[0].data['unread_count'], 0)
        self.assertEqual(events[0].data['scope'], 'all')
        # «Отметить все» must never carry an unbounded id list.
        self.assertNotIn('notification_ids', events[0].data)

    def test_a_read_event_targets_only_its_owner(self):
        notification = self._make_notification('owner:1')

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                mark_notifications_read(
                    self.otk_user, scope='single', notification_ids=[notification.pk]
                )

        _event, targets = publisher.published[0]
        self.assertEqual(target_keys(targets), [f'user:{self.otk_user.pk}'])

    def test_repeating_a_read_action_publishes_nothing(self):
        notification = self._make_notification('idempotent:1')
        with self.captureOnCommitCallbacks(execute=True):
            mark_notifications_read(
                self.otk_user, scope='single', notification_ids=[notification.pk]
            )

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                mark_notifications_read(
                    self.otk_user, scope='single', notification_ids=[notification.pk]
                )

        self.assertEqual(publisher.published, [])

    def test_a_foreign_notification_is_never_marked_or_announced(self):
        foreign = Notification.objects.create(
            recipient=self.to_user,
            event_type=Notification.EventType.COMMENT_ADDED,
            title='Чужое',
            message='Чужое',
            related_act=self.act,
            deduplication_key='foreign:1',
        )

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    reverse('notifications:mark_read_bulk'), {'ids': [foreign.pk]}
                )

        foreign.refresh_from_db()
        self.assertFalse(foreign.is_read)
        self.assertEqual(publisher.published, [])


class ActEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_a_status_change_publishes_one_event_with_both_status_codes(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        events = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_type, 'act')
        self.assertEqual(events[0].resource_id, act.pk)
        self.assertEqual(events[0].data['from_status_code'], 'CREATED_OTK')
        self.assertEqual(events[0].data['to_status_code'], 'KO_REVIEW')
        self.assertEqual(events[0].data['actor_id'], self.otk_user.pk)

    def test_every_transition_uses_the_same_event_type(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)
            act.refresh_from_db()
            with self.captureOnCommitCallbacks(execute=True):
                apply_ko_decision(
                    act, self.ko_user, [(None, Act.KoDecision.PROHIBIT_USE, 'Решение')]
                )

        status_events = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)
        self.assertEqual(len(status_events), 2)
        self.assertEqual(
            [(event.data['from_status_code'], event.data['to_status_code']) for event in status_events],
            [('CREATED_OTK', 'KO_REVIEW'), ('KO_REVIEW', 'TO_ANALYSIS')],
        )

    def test_a_status_change_reaches_the_routed_users_and_the_act_target(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        _event, targets = publisher.published[
            [event.event_type for event in publisher.events].index(
                RealtimeEventType.ACT_STATUS_CHANGED
            )
        ]
        keys = target_keys(targets)
        # KO users are notified of this transition; the author is a participant.
        self.assertIn(f'user:{self.ko_user.pk}', keys)
        self.assertIn(f'user:{self.otk_user.pk}', keys)
        self.assertIn(f'act:{act.pk}', keys)
        # An unrelated OTK user is neither routed nor a participant.
        self.assertNotIn(f'user:{self.outsider.pk}', keys)

    def test_act_creation_does_not_publish_a_status_change(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                self.make_act(self.status_created)

        self.assertEqual(
            publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED), []
        )

    def test_editing_an_act_publishes_act_updated(self):
        act = self.make_act(self.status_created)
        ActDefect.objects.create(
            act=act,
            defect_type=self.defect_type,
            operation=self.operation,
            workshop=ActDefect.Workshop.MP_SHOP,
            znp_number='1-1',
            party_number='2-2',
            mp_type='OL',
            checked_quantity=10,
            nonconforming_quantity=1,
            description='Исходное описание',
            detected_at=timezone.localdate(),
        )
        self.client.force_login(self.otk_user)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse('acts:edit', args=[act.pk]), self._edit_payload(act)
                )

        self.assertEqual(response.status_code, 302)
        events = publisher.events_of_type(RealtimeEventType.ACT_UPDATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_id, act.pk)
        self.assertEqual(events[0].data['status_code'], 'CREATED_OTK')

    def _edit_payload(self, act):
        defect = act.defects.first()
        return {
            'customer': 'Заказчик',
            'order_number': '100-3',
            'nomenclature': 'Катушка-А',
            'kd_designation': 'КД-103',
            'defects-TOTAL_FORMS': '1',
            'defects-INITIAL_FORMS': '1',
            'defects-MIN_NUM_FORMS': '1',
            'defects-MAX_NUM_FORMS': '1000',
            'defects-0-id': defect.pk,
            'defects-0-workshop': ActDefect.Workshop.TRANSFORMERS_SHOP,
            'defects-0-defect_type': self.defect_type.pk,
            'defects-0-operation': self.operation.pk,
            'defects-0-mp_type': 'OL',
            'defects-0-znp_number': '1-1',
            'defects-0-party_number': '2-2',
            'defects-0-checked_quantity': '10',
            'defects-0-nonconforming_quantity': '1',
            'defects-0-description': 'Обновлённое описание',
            'defects-0-detected_at': timezone.localdate().isoformat(),
        }


class CommentEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_adding_a_comment_publishes_one_event_without_its_text(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                comment = add_act_comment(act, self.otk_user, 'Секретный текст комментария')

        events = publisher.events_of_type(RealtimeEventType.COMMENT_CREATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_id, comment.pk)
        self.assertEqual(events[0].data['act_id'], act.pk)
        self.assertEqual(events[0].data['author_id'], self.otk_user.pk)
        self.assertNotIn('Секретный текст', events[0].as_json())

    def test_a_mandatory_return_comment_publishes_exactly_one_comment_event(self):
        act = self.make_act(self.status_ko)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                return_to_otk(act, self.ko_user, 'Причина возврата')

        comment_events = publisher.events_of_type(RealtimeEventType.COMMENT_CREATED)
        self.assertEqual(len(comment_events), 1)
        status_events = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)
        self.assertEqual(len(status_events), 1)

    def test_a_comment_targets_only_act_participants(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                add_act_comment(act, self.otk_user, 'Комментарий')

        _event, targets = publisher.published[
            [event.event_type for event in publisher.events].index(
                RealtimeEventType.COMMENT_CREATED
            )
        ]
        keys = target_keys(targets)
        self.assertIn(f'user:{self.otk_user.pk}', keys)
        self.assertIn(f'act:{act.pk}', keys)
        self.assertNotIn(f'user:{self.outsider.pk}', keys)


class TaskEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def _approve(self, publisher_context):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)
        with publisher_context:
            with self.captureOnCommitCallbacks(execute=True):
                approve_act(act, self.otk_user)
        return act

    def test_approval_publishes_one_task_created_event_per_task(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                approve_act(act, self.otk_user)

        events = publisher.events_of_type(RealtimeEventType.TASK_CREATED)
        tasks = list(Task.objects.filter(act=act))
        self.assertEqual(len(events), len(tasks))
        self.assertEqual(events[0].resource_id, tasks[0].pk)
        self.assertEqual(events[0].data['act_id'], act.pk)
        self.assertEqual(events[0].data['status_code'], 'IN_PROGRESS')
        self.assertEqual(events[0].data['assignee_count'], 1)

    def test_task_created_is_published_only_after_assignees_exist(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act, assignees=[self.to_user, self.ko_user])

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                approve_act(act, self.otk_user)

        event, targets = next(
            item for item in publisher.published
            if item[0].event_type == RealtimeEventType.TASK_CREATED
        )
        task = Task.objects.get(pk=event.resource_id)
        self.assertEqual(TaskAssignee.objects.filter(task=task).count(), 2)
        self.assertEqual(event.data['assignee_count'], 2)
        keys = target_keys(targets)
        self.assertIn(f'user:{self.to_user.pk}', keys)
        self.assertIn(f'user:{self.ko_user.pk}', keys)
        self.assertIn(f'act:{act.pk}', keys)
        self.assertNotIn(f'user:{self.outsider.pk}', keys)

    def test_approval_also_publishes_the_archive_status_change(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                approve_act(act, self.otk_user)

        status_events = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0].data['to_status_code'], 'ARCHIVED')

    def test_completing_a_task_publishes_one_completed_event(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)
        with self.captureOnCommitCallbacks(execute=True):
            approve_act(act, self.otk_user)
        task = Task.objects.get(act=act)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                complete_task(task, self.to_user, 'Выполнено')

        events = publisher.events_of_type(RealtimeEventType.TASK_COMPLETED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_id, task.pk)
        self.assertEqual(events[0].data['status_code'], 'COMPLETED')
        self.assertEqual(events[0].data['completed_by_id'], self.to_user.pk)

    def test_a_second_completion_is_refused_and_publishes_nothing(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)
        with self.captureOnCommitCallbacks(execute=True):
            approve_act(act, self.otk_user)
        task = Task.objects.get(act=act)
        with self.captureOnCommitCallbacks(execute=True):
            complete_task(task, self.to_user, 'Выполнено')

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                from tasks.services import TaskWorkflowError

                with self.assertRaises(TaskWorkflowError):
                    complete_task(task, self.to_user, 'Повторно')

            self.assertEqual(publisher.published, [])

    def test_task_payload_carries_no_task_text(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                approve_act(act, self.otk_user)

        payload = publisher.events_of_type(RealtimeEventType.TASK_CREATED)[0].as_json()
        self.assertNotIn('Корректирующее мероприятие', payload)
        self.assertNotIn(self.to_user.username, payload)


class NoDuplicateEventsTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_one_workflow_operation_publishes_each_fact_once(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        counts = {}
        for event in publisher.events:
            counts[event.event_type] = counts.get(event.event_type, 0) + 1
        self.assertEqual(counts.get(RealtimeEventType.ACT_STATUS_CHANGED), 1)
        # The KO user gets exactly one notification, hence one created event.
        self.assertEqual(counts.get(RealtimeEventType.NOTIFICATION_CREATED), 1)
        self.assertEqual(
            len({event.event_id for event in publisher.events}), len(publisher.events)
        )

    def test_a_notification_event_is_not_emitted_twice_by_the_workflow(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        notification_events = publisher.events_of_type(
            RealtimeEventType.NOTIFICATION_CREATED
        )
        resource_ids = [event.resource_id for event in notification_events]
        self.assertEqual(len(resource_ids), len(set(resource_ids)))
        self.assertEqual(
            set(resource_ids),
            set(Notification.objects.filter(related_act=act).values_list('pk', flat=True)),
        )
