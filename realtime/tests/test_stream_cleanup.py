import asyncio
import logging

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.sse import decode_message, event_stream, redis_is_reachable

from .fakes import (
    FakeAsyncRedis,
    connection_error,
    message,
    subscribe_confirmation,
    timeout_error,
)


SECRET_URL = 'redis://appuser:s3cr3t-redis-password@redis.internal:6379/2'
USER_CHANNEL = 'demo:realtime:user:7'


def build_event(resource_id=11, data=None):
    return RealtimeEvent(
        event_type=RealtimeEventType.NOTIFICATION_CREATED,
        resource_type='notification',
        resource_id=resource_id,
        data=data if data is not None else {'act_id': 3, 'recipient_id': 5, 'actor_id': None},
    )


async def _collect(client, limit=3, user_id=7):
    frames = []
    stream = event_stream(user_id, client_factory=lambda: client, max_messages=limit)
    async for frame in stream:
        frames.append(frame)
        if len(frames) > limit + 1:
            break
    await stream.aclose()
    return frames


def collect(client, limit=3, user_id=7):
    return async_to_sync(_collect)(client, limit, user_id)


@override_settings(
    REALTIME_CHANNEL_PREFIX='demo:realtime',
    REALTIME_HEARTBEAT_SECONDS=1,
    REALTIME_RECONNECT_DELAY_MS=3000,
)
class StreamBehaviourTests(SimpleTestCase):
    def test_the_stream_opens_with_a_retry_frame_and_subscribes(self):
        client = FakeAsyncRedis(script=[None])

        frames = collect(client, limit=1)

        self.assertEqual(frames[0], 'retry: 3000\n\n')
        self.assertEqual(client.pubsub_instance.subscribed, [USER_CHANNEL])

    def test_a_timeout_produces_a_heartbeat(self):
        client = FakeAsyncRedis(script=[None, None])

        frames = collect(client, limit=2)

        self.assertEqual(frames[1], ': heartbeat\n\n')
        self.assertEqual(frames[2], ': heartbeat\n\n')

    def test_subscribe_confirmations_are_ignored(self):
        event = build_event()
        client = FakeAsyncRedis(
            script=[
                subscribe_confirmation(USER_CHANNEL),
                message(USER_CHANNEL, event.as_compact_json()),
            ]
        )

        frames = collect(client, limit=1)

        self.assertEqual(len(frames), 2)
        self.assertIn(f'id: {event.event_id}', frames[1])

    def test_a_malformed_message_is_skipped_without_closing_the_stream(self):
        event = build_event()
        client = FakeAsyncRedis(
            script=[
                message(USER_CHANNEL, '{not json'),
                message(USER_CHANNEL, event.as_compact_json()),
            ]
        )

        with self.assertLogs('realtime', level=logging.WARNING) as captured:
            frames = collect(client, limit=1)

        self.assertEqual(len(frames), 2)
        self.assertIn(f'id: {event.event_id}', frames[1])
        self.assertIn('dropped a message', captured.output[0])

    def test_an_unknown_event_type_is_skipped(self):
        client = FakeAsyncRedis(
            script=[
                message(USER_CHANNEL, '{"schema_version":1,"event_id":"x","event_type":"act.exploded"}'),
                None,
            ]
        )

        with self.assertLogs('realtime', level=logging.WARNING):
            frames = collect(client, limit=1)

        self.assertEqual(frames[1], ': heartbeat\n\n')

    def test_a_message_carrying_targets_is_skipped(self):
        payload = build_event().as_dict()
        payload['targets'] = ['user:7']
        import json

        client = FakeAsyncRedis(script=[message(USER_CHANNEL, json.dumps(payload)), None])

        with self.assertLogs('realtime', level=logging.WARNING) as captured:
            frames = collect(client, limit=1)

        self.assertEqual(frames[1], ': heartbeat\n\n')
        self.assertIn('посторонние поля', captured.output[0])

    @override_settings(REALTIME_MAX_EVENT_BYTES=256)
    def test_an_oversized_message_is_skipped(self):
        oversized = build_event(data={'note': 'x' * 2000})
        client = FakeAsyncRedis(
            script=[message(USER_CHANNEL, oversized.as_compact_json()), None]
        )

        with self.assertLogs('realtime', level=logging.WARNING) as captured:
            frames = collect(client, limit=1)

        self.assertEqual(frames[1], ': heartbeat\n\n')
        self.assertIn('превышает', captured.output[0])
        self.assertNotIn('xxxx', captured.output[0])

    @override_settings(REALTIME_MAX_EVENT_BYTES=256)
    def test_heartbeats_continue_after_a_skipped_message(self):
        oversized = build_event(data={'note': 'x' * 2000})
        client = FakeAsyncRedis(
            script=[
                message(USER_CHANNEL, oversized.as_compact_json()),
                None,
                None,
            ]
        )

        with self.assertLogs('realtime', level=logging.WARNING):
            frames = collect(client, limit=2)

        self.assertEqual(frames[1], ': heartbeat\n\n')
        self.assertEqual(frames[2], ': heartbeat\n\n')

    def test_decode_message_returns_none_for_rubbish(self):
        with self.assertLogs('realtime', level=logging.WARNING):
            self.assertIsNone(decode_message(b'\xff\xfe', max_bytes=1024))


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime', REALTIME_HEARTBEAT_SECONDS=1)
class StreamCleanupTests(SimpleTestCase):
    def test_finishing_the_stream_unsubscribes_and_closes_everything(self):
        client = FakeAsyncRedis(script=[None])

        collect(client, limit=1)

        self.assertEqual(client.pubsub_instance.unsubscribed, [USER_CHANNEL])
        self.assertTrue(client.pubsub_instance.closed)
        self.assertTrue(client.closed)

    def test_cancellation_releases_the_pubsub_and_the_client(self):
        client = FakeAsyncRedis(script=[asyncio.CancelledError()])

        async def run():
            stream = event_stream(7, client_factory=lambda: client)
            frames = []
            with self.assertRaises(asyncio.CancelledError):
                async for frame in stream:
                    frames.append(frame)
            return frames

        frames = async_to_sync(run)()

        self.assertEqual(frames, ['retry: 3000\n\n'])
        self.assertEqual(client.pubsub_instance.unsubscribed, [USER_CHANNEL])
        self.assertTrue(client.pubsub_instance.closed)
        self.assertTrue(client.closed)

    def test_a_closed_client_generator_still_cleans_up(self):
        client = FakeAsyncRedis(script=[None, None, None])

        async def run():
            stream = event_stream(7, client_factory=lambda: client)
            frames = []
            async for frame in stream:
                frames.append(frame)
                if len(frames) >= 2:
                    break
            # A disconnecting browser closes the generator exactly like this.
            await stream.aclose()
            return frames

        async_to_sync(run)()

        self.assertEqual(client.pubsub_instance.unsubscribed, [USER_CHANNEL])
        self.assertTrue(client.pubsub_instance.closed)
        self.assertTrue(client.closed)

    def test_a_redis_disconnect_ends_the_stream_without_leaking_resources(self):
        client = FakeAsyncRedis(script=[None, connection_error()])

        with self.assertLogs('realtime', level=logging.WARNING) as captured:
            frames = collect(client, limit=5)

        self.assertEqual(frames[-1], ': heartbeat\n\n')
        self.assertIn('realtime.redis_disconnected', captured.output[0])
        self.assertEqual(client.pubsub_instance.unsubscribed, [USER_CHANNEL])
        self.assertTrue(client.pubsub_instance.closed)
        self.assertTrue(client.closed)

    def test_a_timeout_error_ends_the_stream_controllably(self):
        client = FakeAsyncRedis(script=[timeout_error()])

        with self.assertLogs('realtime', level=logging.WARNING):
            frames = collect(client, limit=5)

        self.assertEqual(frames, ['retry: 3000\n\n'])
        self.assertTrue(client.closed)

    def test_cleanup_survives_failures_in_unsubscribe_and_close(self):
        client = FakeAsyncRedis(script=[None])
        client.pubsub_instance.unsubscribe_error = connection_error()
        client.pubsub_instance.close_error = connection_error()
        client.close_error = connection_error()

        # Cleanup must never raise, even when every teardown step fails.
        frames = collect(client, limit=1)

        self.assertEqual(frames[0], 'retry: 3000\n\n')

    @override_settings(REALTIME_REDIS_URL=SECRET_URL)
    def test_a_disconnect_log_never_contains_credentials(self):
        client = FakeAsyncRedis(
            script=[connection_error(f'Error connecting to {SECRET_URL}')]
        )

        with self.assertLogs('realtime', level=logging.WARNING) as captured:
            collect(client, limit=2)

        logged = '\n'.join(captured.output)
        self.assertNotIn('s3cr3t-redis-password', logged)
        self.assertNotIn('appuser', logged)
        self.assertNotIn(SECRET_URL, logged)
        self.assertIn('redis.internal', logged)


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
class ConnectivityProbeTests(SimpleTestCase):
    def test_a_healthy_client_reports_reachable_and_is_closed(self):
        client = FakeAsyncRedis()

        reachable, reason = async_to_sync(redis_is_reachable)(client_factory=lambda: client)

        self.assertTrue(reachable)
        self.assertEqual(reason, '')
        self.assertTrue(client.closed)

    def test_a_connection_error_reports_unreachable(self):
        client = FakeAsyncRedis(ping_error=connection_error())

        reachable, reason = async_to_sync(redis_is_reachable)(client_factory=lambda: client)

        self.assertFalse(reachable)
        self.assertIn('ConnectionError', reason)
        self.assertTrue(client.closed)

    @override_settings(REALTIME_REDIS_CONNECT_TIMEOUT_SECONDS=0.1)
    def test_a_hanging_ping_reports_unreachable(self):
        client = FakeAsyncRedis(ping_delay=5)

        reachable, reason = async_to_sync(redis_is_reachable)(client_factory=lambda: client)

        self.assertFalse(reachable)
        self.assertIn('PING', reason)
        self.assertTrue(client.closed)

    @override_settings(REALTIME_REDIS_URL=SECRET_URL)
    def test_the_probe_reason_never_contains_credentials(self):
        client = FakeAsyncRedis(
            ping_error=connection_error(f'Error connecting to {SECRET_URL}')
        )

        _reachable, reason = async_to_sync(redis_is_reachable)(client_factory=lambda: client)

        self.assertNotIn('s3cr3t-redis-password', reason)
        self.assertNotIn('appuser', reason)
        self.assertNotIn(SECRET_URL, reason)
