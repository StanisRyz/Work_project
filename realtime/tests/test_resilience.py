import logging

from django.test import TestCase, override_settings

from acts.models import Act, ActComment
from acts.services import add_act_comment, approve_act, send_to_ko
from notifications.models import Notification
from notifications.services import create_notifications
from realtime.backends import RealtimePublisherError
from realtime.publisher import reset_publisher
from realtime.testing import capture_realtime_events, failing_realtime_publisher
from tasks.models import Task
from tasks.services import complete_task

from .base import RealtimeFixtureMixin


class FailingPublisherTests(RealtimeFixtureMixin, TestCase):
    """A broken transport must never undo an already committed operation."""

    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def test_a_saved_act_transition_survives_a_publisher_failure(self):
        act = self.make_act(self.status_created)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.captureOnCommitCallbacks(execute=True):
                    send_to_ko(act, self.otk_user)

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')

    def test_a_saved_comment_survives_a_publisher_failure(self):
        act = self.make_act(self.status_created)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.captureOnCommitCallbacks(execute=True):
                    comment = add_act_comment(act, self.otk_user, 'Текст комментария')

        self.assertTrue(ActComment.objects.filter(pk=comment.pk).exists())

    def test_a_saved_notification_survives_a_publisher_failure(self):
        act = self.make_act(self.status_created)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.captureOnCommitCallbacks(execute=True):
                    created = create_notifications(
                        event_type=Notification.EventType.COMMENT_ADDED,
                        act=act,
                        actor=self.ko_user,
                        recipients=[self.otk_user],
                        source_key='resilience:1',
                    )

        self.assertEqual(len(created), 1)
        self.assertTrue(Notification.objects.filter(pk=created[0].pk).exists())

    def test_a_saved_task_survives_a_publisher_failure(self):
        act = self.make_act(self.status_otk_review)
        act.to_analysis_by = self.to_user
        act.save(update_fields=['to_analysis_by'])
        self.make_analysis(act)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.captureOnCommitCallbacks(execute=True):
                    approve_act(act, self.otk_user)

        self.assertEqual(Task.objects.filter(act=act).count(), 1)
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'ARCHIVED')

    def test_a_completed_task_survives_a_publisher_failure(self):
        act = self.make_act(self.status_otk_review)
        self.make_analysis(act)
        with self.captureOnCommitCallbacks(execute=True):
            approve_act(act, self.otk_user)
        task = Task.objects.get(act=act)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.captureOnCommitCallbacks(execute=True):
                    complete_task(task, self.to_user, 'Выполнено')

        task.refresh_from_db()
        self.assertEqual(task.status.code, 'COMPLETED')

    def test_the_failure_is_logged_with_diagnostic_context(self):
        act = self.make_act(self.status_created)

        with failing_realtime_publisher():
            with self.assertLogs('realtime', level=logging.ERROR) as captured:
                with self.captureOnCommitCallbacks(execute=True):
                    send_to_ko(act, self.otk_user)

        message = captured.output[0]
        self.assertIn('act.status_changed', message)
        self.assertIn(f'act:{act.pk}', message)
        self.assertIn('RealtimePublisherError', message)
        self.assertIn('FailingRealtimePublisher', message)

    def test_the_exception_is_available_when_fail_silently_is_off(self):
        act = self.make_act(self.status_created)

        with failing_realtime_publisher(fail_silently=False):
            with self.assertLogs('realtime', level=logging.ERROR):
                with self.assertRaises(RealtimePublisherError):
                    with self.captureOnCommitCallbacks(execute=True):
                        send_to_ko(act, self.otk_user)

        # The database work is committed before the callback runs, so the
        # transition itself is unaffected by the surfaced exception.
        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')


class NoopBackendTests(RealtimeFixtureMixin, TestCase):
    """The shipped default must be indistinguishable from no real-time at all."""

    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()

    def tearDown(self):
        reset_publisher()

    @override_settings(
        REALTIME_ENABLED=True,
        REALTIME_PUBLISHER_BACKEND='realtime.backends.NoopRealtimePublisher',
    )
    def test_the_noop_backend_changes_no_business_outcome(self):
        reset_publisher()
        act = self.make_act(self.status_created)

        with self.captureOnCommitCallbacks(execute=True):
            send_to_ko(act, self.otk_user)
            add_act_comment(act, self.otk_user, 'Комментарий')

        act.refresh_from_db()
        self.assertEqual(act.status.code, 'KO_REVIEW')
        self.assertEqual(ActComment.objects.filter(act=act).count(), 1)

    @override_settings(REALTIME_ENABLED=False)
    def test_disabled_realtime_registers_no_commit_callbacks(self):
        act = self.make_act(self.status_created)

        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            send_to_ko(act, self.otk_user)

        self.assertEqual(callbacks, [])
        self.assertEqual(Act.objects.filter(status__code='KO_REVIEW').count(), 1)

    def test_capture_backend_does_not_hide_errors_by_itself(self):
        act = self.make_act(self.status_created)

        with capture_realtime_events(fail_silently=False) as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                send_to_ko(act, self.otk_user)

        self.assertTrue(publisher.published)
