from unittest import mock

from django.test import SimpleTestCase, override_settings

from realtime import transport
from realtime.backends import NoopRealtimePublisher
from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.publisher import dispatch_event, reset_publisher, set_publisher
from realtime.targets import user_target


SECRET_URL = 'redis://appuser:s3cr3t-redis-password@redis.internal:6379/2'


def build_event():
    return RealtimeEvent(
        event_type=RealtimeEventType.ACT_UPDATED,
        resource_type='act',
        resource_id=3,
        data={'status_code': 'CREATED_OTK'},
    )


class SafeLocationTests(SimpleTestCase):
    def test_credentials_are_stripped_from_the_location(self):
        self.assertEqual(
            transport.safe_redis_location(SECRET_URL), 'redis://redis.internal:6379/2'
        )

    def test_a_url_without_credentials_is_reported_as_is(self):
        self.assertEqual(
            transport.safe_redis_location('redis://127.0.0.1:6379/0'),
            'redis://127.0.0.1:6379/0',
        )

    def test_an_unparsable_url_is_fully_redacted(self):
        self.assertEqual(transport.safe_redis_location('not-a-url'), transport.REDACTED)

    def test_sanitize_removes_the_url_username_and_password(self):
        text = f'failed for {SECRET_URL} as appuser with s3cr3t-redis-password'

        cleaned = transport.sanitize(text, SECRET_URL)

        self.assertNotIn('s3cr3t-redis-password', cleaned)
        self.assertNotIn('appuser', cleaned)
        self.assertNotIn(SECRET_URL, cleaned)

    @override_settings(REALTIME_REDIS_URL=SECRET_URL)
    def test_describe_failure_is_log_safe_but_still_useful(self):
        description = transport.describe_failure(
            ConnectionError(f'cannot reach {SECRET_URL}')
        )

        self.assertNotIn('s3cr3t-redis-password', description)
        self.assertIn('ConnectionError', description)
        self.assertIn('redis.internal', description)


class DisabledRealtimeTests(SimpleTestCase):
    """With real-time off, nothing may reach out to Redis."""

    def tearDown(self):
        reset_publisher()

    @override_settings(
        REALTIME_ENABLED=False,
        REALTIME_PUBLISHER_BACKEND='realtime.backends.NoopRealtimePublisher',
    )
    def test_a_disabled_dispatch_never_builds_a_client(self):
        reset_publisher()

        with mock.patch.object(transport, 'sync_client') as factory:
            self.assertFalse(dispatch_event(build_event(), [user_target(1)]))

        factory.assert_not_called()

    def test_the_noop_publisher_never_builds_a_client(self):
        publisher = set_publisher(NoopRealtimePublisher())

        with override_settings(REALTIME_ENABLED=True):
            with mock.patch.object(transport, 'sync_client') as factory:
                dispatch_event(build_event(), [user_target(1)])

        factory.assert_not_called()
        self.assertIsInstance(publisher, NoopRealtimePublisher)

    @override_settings(REALTIME_ENABLED=False)
    def test_no_redis_connection_pool_is_created_while_disabled(self):
        transport.close_sync_pool()

        dispatch_event(build_event(), [user_target(1)])

        self.assertIsNone(transport._sync_pool)


class ConnectionPoolTests(SimpleTestCase):
    def tearDown(self):
        transport.close_sync_pool()

    @override_settings(REALTIME_REDIS_URL='redis://127.0.0.1:6379/0')
    def test_the_pool_is_reused_between_clients(self):
        # Building a pool does not connect: no Redis server is needed here.
        first = transport.sync_connection_pool()
        second = transport.sync_connection_pool()

        self.assertIs(first, second)

    def test_changing_the_url_rebuilds_the_pool(self):
        with override_settings(REALTIME_REDIS_URL='redis://127.0.0.1:6379/0'):
            first = transport.sync_connection_pool()
        with override_settings(REALTIME_REDIS_URL='redis://127.0.0.1:6379/1'):
            second = transport.sync_connection_pool()

        self.assertIsNot(first, second)

    @override_settings(REALTIME_REDIS_URL='')
    def test_an_empty_url_is_a_configuration_error(self):
        from django.core.exceptions import ImproperlyConfigured

        with self.assertRaisesMessage(ImproperlyConfigured, 'REALTIME_REDIS_URL'):
            transport.get_redis_url()
