from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings

from acts.models import Act, ActHistoryEvent
from acts.services import ActWorkflowError, send_to_ko
from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.publisher import publish_after_commit, realtime_enabled
from realtime.targets import RealtimeTargetError, user_target
from realtime.testing import capture_realtime_events

from .base import RealtimeFixtureMixin


def build_event(resource_id=1):
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_UPDATED,
        resource_type='act',
        resource_id=resource_id,
        data={},
    )


class AfterCommitTests(TestCase):
    def test_a_committed_transaction_publishes_exactly_once(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                publish_after_commit(build_event(), [user_target(1)])
                self.assertEqual(publisher.published, [], 'опубликовано до commit')

            self.assertEqual(len(publisher.published), 1)

    def test_a_rolled_back_transaction_publishes_nothing(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    publish_after_commit(build_event(), [user_target(1)])
                    transaction.set_rollback(True)

            self.assertEqual(publisher.published, [])

    def test_callbacks_keep_their_registration_order(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                for resource_id in (1, 2, 3):
                    publish_after_commit(build_event(resource_id), [user_target(1)])

            self.assertEqual(
                [event.resource_id for event in publisher.events], [1, 2, 3]
            )

    def test_each_publication_carries_its_own_event_id(self):
        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                for resource_id in (1, 2, 3):
                    publish_after_commit(build_event(resource_id), [user_target(1)])

            identifiers = {event.event_id for event in publisher.events}
            self.assertEqual(len(identifiers), 3)

    def test_invalid_targets_fail_at_the_call_site_not_inside_the_callback(self):
        with capture_realtime_events():
            with self.assertRaises(RealtimeTargetError):
                publish_after_commit(build_event(), ['user:1'])

    def test_nothing_is_registered_while_realtime_is_disabled(self):
        with override_settings(REALTIME_ENABLED=False):
            self.assertFalse(realtime_enabled())
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                self.assertFalse(publish_after_commit(build_event(), [user_target(1)]))

            self.assertEqual(callbacks, [])

    def test_an_event_without_targets_registers_no_callback(self):
        with capture_realtime_events():
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                self.assertFalse(publish_after_commit(build_event(), []))

            self.assertEqual(callbacks, [])


class WorkflowTransactionTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_a_successful_transition_publishes_one_status_event(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        status_events = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)
        self.assertEqual(len(status_events), 1)
        self.assertEqual(status_events[0].data['from_status_code'], 'CREATED_OTK')
        self.assertEqual(status_events[0].data['to_status_code'], 'KO_REVIEW')

    def test_a_rejected_repeat_transition_publishes_nothing(self):
        act = self.make_act(self.status_created)
        with self.captureOnCommitCallbacks(execute=True):
            send_to_ko(act, self.otk_user)
        act.refresh_from_db()

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ActWorkflowError):
                    # The act already left CREATED_OTK: the second, stale
                    # request must be refused without publishing anything.
                    send_to_ko(act, self.otk_user)

            self.assertEqual(publisher.published, [])

    def test_an_event_is_only_built_after_its_history_row_exists(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        event = publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)[0]
        history = ActHistoryEvent.objects.get(pk=event.data['history_event_id'])
        self.assertEqual(history.act_id, act.pk)
        self.assertEqual(history.event_type, ActHistoryEvent.EventType.SENT_TO_KO)

    def test_a_failed_workflow_operation_rolls_the_event_back_with_the_data(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(ActWorkflowError):
                    # Not the author and not a full-access role.
                    send_to_ko(act, self.outsider)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(publisher.published, [])


class RollbackWithRealDatabaseTests(RealtimeFixtureMixin, TransactionTestCase):
    """A real rollback, not a savepoint one, must also publish nothing."""

    reset_sequences = True

    def setUp(self):
        self.setUpRealtimeData()

    def test_an_exception_after_a_publication_prevents_it(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            with self.assertRaises(RuntimeError):
                with transaction.atomic():
                    send_to_ko(act, self.otk_user)
                    raise RuntimeError('сбой после перехода')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'CREATED_OTK')
        self.assertEqual(publisher.published, [])
        self.assertEqual(Act.objects.filter(status__code='KO_REVIEW').count(), 0)

    def test_a_real_commit_publishes_the_event(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events() as publisher:
            send_to_ko(act, self.otk_user)

        self.assertEqual(
            len(publisher.events_of_type(RealtimeEventType.ACT_STATUS_CHANGED)), 1
        )
