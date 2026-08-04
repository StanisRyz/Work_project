import logging
from unittest import mock

from django.test import SimpleTestCase, override_settings

from realtime.backends import RealtimePublisherError, RedisRealtimePublisher
from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.publisher import dispatch_event, reset_publisher, set_publisher
from realtime.targets import act_target, user_target

from .fakes import FakeSyncRedis, connection_error, timeout_error


SECRET_URL = 'redis://appuser:s3cr3t-redis-password@redis.internal:6379/2'


def build_event(resource_id=3, data=None):
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_STATUS_CHANGED,
        resource_type='act',
        resource_id=resource_id,
        data=data if data is not None else {'from_status_code': 'CREATED_OTK'},
    )


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
class RedisPublisherTests(SimpleTestCase):
    def setUp(self):
        self.client = FakeSyncRedis()
        self.publisher = RedisRealtimePublisher(client_factory=lambda: self.client)

    def test_one_event_is_serialized_once_and_reused_for_every_channel(self):
        event = build_event()

        with mock.patch.object(
            RealtimeEvent, 'as_compact_json', wraps=event.as_compact_json
        ) as serializer:
            self.publisher.publish(event, (user_target(7), act_target(3)))

        self.assertEqual(serializer.call_count, 1)
        payloads = {payload for _channel, payload in self.client.published}
        self.assertEqual(len(payloads), 1)

    def test_each_target_gets_its_own_prefixed_channel(self):
        self.publisher.publish(build_event(), (user_target(7), act_target(3)))

        self.assertEqual(
            [channel for channel, _payload in self.client.published],
            ['demo:realtime:act:3', 'demo:realtime:user:7'],
        )

    def test_the_configured_prefix_is_applied(self):
        with override_settings(REALTIME_CHANNEL_PREFIX='other:ns'):
            self.publisher.publish(build_event(), (user_target(7),))

        self.assertEqual(self.client.published[0][0], 'other:ns:user:7')

    def test_duplicate_targets_do_not_publish_twice(self):
        self.publisher.publish(
            build_event(), (user_target(7), user_target(7), user_target(7))
        )

        self.assertEqual(len(self.client.published), 1)

    def test_zero_subscribers_is_not_an_error(self):
        self.client.subscribers = 0

        self.assertIsNone(self.publisher.publish(build_event(), (user_target(7),)))
        self.assertEqual(len(self.client.published), 1)

    def test_the_payload_is_the_compact_event_json(self):
        event = build_event()

        self.publisher.publish(event, (user_target(7),))

        _channel, payload = self.client.published[0]
        self.assertEqual(payload, event.as_compact_json().encode('utf-8'))
        self.assertNotIn(b'targets', payload)
        self.assertNotIn(b'demo:realtime', payload)

    def test_an_oversized_event_is_dropped_before_publishing(self):
        event = build_event(data={'note': 'x' * 4000})

        with override_settings(REALTIME_MAX_EVENT_BYTES=256):
            with self.assertLogs('realtime', level=logging.WARNING) as captured:
                self.publisher.publish(event, (user_target(7),))

        self.assertEqual(self.client.published, [])
        self.assertIn('dropped before publish', captured.output[0])
        self.assertNotIn('xxxx', captured.output[0])

    def test_no_targets_means_no_call_to_redis(self):
        self.assertIsNone(self.publisher.publish(build_event(), ()))
        self.assertEqual(self.client.published, [])


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
class RedisPublisherFailureTests(SimpleTestCase):
    def tearDown(self):
        reset_publisher()

    def _publisher_raising(self, error):
        client = FakeSyncRedis(publish_error=error)
        return RedisRealtimePublisher(client_factory=lambda: client)

    def test_a_connection_error_becomes_a_publisher_error(self):
        publisher = self._publisher_raising(connection_error())

        with self.assertRaises(RealtimePublisherError):
            publisher.publish(build_event(), (user_target(7),))

    def test_a_timeout_becomes_a_publisher_error(self):
        publisher = self._publisher_raising(timeout_error())

        with self.assertRaises(RealtimePublisherError):
            publisher.publish(build_event(), (user_target(7),))

    @override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=True)
    def test_a_failure_is_handled_by_the_existing_fail_silently_policy(self):
        set_publisher(self._publisher_raising(connection_error()))

        with self.assertLogs('realtime', level=logging.ERROR) as captured:
            published = dispatch_event(build_event(), (user_target(7),))

        self.assertFalse(published)
        self.assertIn('RealtimePublisherError', captured.output[0])

    @override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=False)
    def test_the_failure_surfaces_when_fail_silently_is_off(self):
        set_publisher(self._publisher_raising(timeout_error()))

        with self.assertLogs('realtime', level=logging.ERROR):
            with self.assertRaises(RealtimePublisherError):
                dispatch_event(build_event(), (user_target(7),))

    @override_settings(REALTIME_ENABLED=True, REALTIME_FAIL_SILENTLY=True)
    def test_the_redis_url_and_password_never_reach_the_log(self):
        error = connection_error(f'Error connecting to {SECRET_URL}')
        set_publisher(self._publisher_raising(error))

        with override_settings(REALTIME_REDIS_URL=SECRET_URL):
            with self.assertLogs('realtime', level=logging.ERROR) as captured:
                with self.assertRaises(RealtimePublisherError) as ctx:
                    RedisRealtimePublisher(
                        client_factory=lambda: FakeSyncRedis(publish_error=error)
                    ).publish(build_event(), (user_target(7),))
                dispatch_event(build_event(), (user_target(7),))

        logged = '\n'.join(captured.output) + str(ctx.exception)
        self.assertNotIn('s3cr3t-redis-password', logged)
        self.assertNotIn(SECRET_URL, logged)
        self.assertNotIn('appuser', logged)
        # The host is still identifiable for diagnosis.
        self.assertIn('redis.internal', str(ctx.exception))
