import logging

from django.test import SimpleTestCase, override_settings

from realtime.backends import (
    CaptureRealtimePublisher,
    FailingRealtimePublisher,
    NoopRealtimePublisher,
    RealtimePublisher,
    RealtimePublisherError,
)
from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.publisher import (
    DEFAULT_BACKEND,
    dispatch_event,
    get_publisher,
    realtime_enabled,
    reset_publisher,
    set_publisher,
)
from realtime.targets import act_target, user_target


def build_event():
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_UPDATED,
        resource_type='act',
        resource_id=3,
        data={'status_code': 'CREATED_OTK'},
    )


class BackendSelectionTests(SimpleTestCase):
    def tearDown(self):
        reset_publisher()

    def test_the_default_backend_sends_nothing(self):
        self.assertEqual(DEFAULT_BACKEND, 'realtime.backends.NoopRealtimePublisher')
        with override_settings(REALTIME_PUBLISHER_BACKEND=DEFAULT_BACKEND):
            reset_publisher()
            publisher = get_publisher()

        self.assertIsInstance(publisher, NoopRealtimePublisher)
        self.assertIsNone(publisher.publish(build_event(), (user_target(1),)))

    def test_the_backend_is_loaded_from_a_dotted_path(self):
        with override_settings(
            REALTIME_PUBLISHER_BACKEND='realtime.backends.CaptureRealtimePublisher'
        ):
            publisher = get_publisher()

        self.assertIsInstance(publisher, CaptureRealtimePublisher)

    def test_the_instance_is_cached_between_calls(self):
        with override_settings(
            REALTIME_PUBLISHER_BACKEND='realtime.backends.CaptureRealtimePublisher'
        ):
            self.assertIs(get_publisher(), get_publisher())

    def test_the_backend_can_be_overridden_and_reset_in_tests(self):
        capture = CaptureRealtimePublisher()

        set_publisher(capture)
        self.assertIs(get_publisher(), capture)

        reset_publisher()
        self.assertIsNot(get_publisher(), capture)

    def test_every_backend_implements_the_single_interface(self):
        for backend in (
            NoopRealtimePublisher,
            CaptureRealtimePublisher,
            FailingRealtimePublisher,
        ):
            with self.subTest(backend=backend.__name__):
                self.assertTrue(issubclass(backend, RealtimePublisher))
                self.assertIn(backend.__name__, backend().label)


class DispatchTests(SimpleTestCase):
    def setUp(self):
        self.publisher = set_publisher(CaptureRealtimePublisher())
        self.addCleanup(reset_publisher)

    @override_settings(REALTIME_ENABLED=False)
    def test_nothing_is_dispatched_while_realtime_is_disabled(self):
        self.assertFalse(realtime_enabled())

        self.assertFalse(dispatch_event(build_event(), [user_target(1)]))
        self.assertEqual(self.publisher.published, [])

    @override_settings(REALTIME_ENABLED=True)
    def test_an_event_reaches_the_backend_with_normalized_targets(self):
        dispatched = dispatch_event(
            build_event(), [user_target(5), user_target(5), act_target(3), None]
        )

        self.assertTrue(dispatched)
        self.assertEqual(len(self.publisher.published), 1)
        _event, targets = self.publisher.published[0]
        self.assertEqual([target.key for target in targets], ['act:3', 'user:5'])

    @override_settings(REALTIME_ENABLED=True)
    def test_an_event_without_targets_is_not_dispatched(self):
        self.assertFalse(dispatch_event(build_event(), []))
        self.assertEqual(self.publisher.published, [])


class DispatchFailureTests(SimpleTestCase):
    def setUp(self):
        set_publisher(FailingRealtimePublisher())
        self.addCleanup(reset_publisher)

    @override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=True)
    def test_a_backend_failure_is_logged_and_swallowed_by_default(self):
        event = build_event()

        with self.assertLogs('realtime', level=logging.ERROR) as captured:
            self.assertFalse(dispatch_event(event, [user_target(1), act_target(3)]))

        message = captured.output[0]
        self.assertIn(str(event.event_id), message)
        self.assertIn('act.updated', message)
        self.assertIn('act:3', message)
        self.assertIn('targets=2', message)
        self.assertIn('FailingRealtimePublisher', message)
        self.assertIn('RealtimePublisherError', message)
        # The payload itself must never be logged.
        self.assertNotIn('CREATED_OTK', message)

    @override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=False)
    def test_the_exception_surfaces_when_fail_silently_is_off(self):
        with self.assertLogs('realtime', level=logging.ERROR):
            with self.assertRaises(RealtimePublisherError):
                dispatch_event(build_event(), [user_target(1)])
