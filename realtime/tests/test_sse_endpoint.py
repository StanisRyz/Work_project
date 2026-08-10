import json
from unittest import mock

from asgiref.sync import async_to_sync

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from realtime import sse, views
from realtime.channels import user_channel
from realtime.events import RealtimeEvent, RealtimeEventType

from .fakes import FakeAsyncRedis, connection_error, message


def build_event(resource_id=11, data=None):
    return RealtimeEvent(
        event_type=RealtimeEventType.NOTIFICATION_CREATED,
        resource_type='notification',
        resource_id=resource_id,
        data=data if data is not None else {'act_id': 3, 'recipient_id': 5, 'actor_id': None},
    )


async def _adrain(response, limit):
    chunks = []
    iterator = response.streaming_content
    async for chunk in iterator:
        chunks.append(chunk.decode('utf-8') if isinstance(chunk, bytes) else chunk)
        if len(chunks) >= limit:
            break
    # Closing the generator is what a disconnecting client does, so cleanup runs.
    await iterator.aclose()
    return chunks


def drain(response, limit=10):
    """Collect frames from the async streaming response, then close it."""
    return async_to_sync(_adrain)(response, limit)


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
class EndpointAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='sse_user', password='demo12345')
        cls.other = User.objects.create_user(username='sse_other', password='demo12345')

    def test_an_anonymous_request_is_rejected_with_401(self):
        with override_settings(REALTIME_ENABLED=True):
            response = self.client.get(reverse('realtime:events'))

        self.assertEqual(response.status_code, 401)
        # Confirms the SSE endpoint never falls back to an HTML login
        # redirect either — it already derived 401 from the session alone.
        self.assertNotIn('Location', response)

    @override_settings(REALTIME_ENABLED=False)
    def test_disabled_realtime_returns_204_without_touching_redis(self):
        self.client.force_login(self.user)

        with mock.patch.object(sse, 'redis_is_reachable') as probe:
            response = self.client.get(reverse('realtime:events'))

        self.assertEqual(response.status_code, 204)
        probe.assert_not_called()

    @override_settings(REALTIME_ENABLED=True)
    def test_a_failed_redis_preflight_returns_503(self):
        self.client.force_login(self.user)
        client = FakeAsyncRedis(ping_error=connection_error())

        with mock.patch.object(views, 'redis_is_reachable', _probe_with(client)):
            response = self.client.get(reverse('realtime:events'))

        self.assertEqual(response.status_code, 503)

    @override_settings(REALTIME_ENABLED=True)
    def test_a_non_get_method_is_refused(self):
        self.client.force_login(self.user)

        response = self.client.post(reverse('realtime:events'))

        self.assertEqual(response.status_code, 405)

    @override_settings(REALTIME_ENABLED=True)
    def test_a_successful_response_streams_with_the_documented_headers(self):
        self.client.force_login(self.user)
        client = FakeAsyncRedis(script=[None])

        with _fake_transport(client):
            response = self.client.get(reverse('realtime:events'))
            drain(response, limit=2)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.streaming)
        self.assertEqual(response['Content-Type'], 'text/event-stream')
        self.assertEqual(response['Cache-Control'], 'no-cache, no-store, no-transform')
        self.assertEqual(response['X-Accel-Buffering'], 'no')
        self.assertEqual(response['Vary'], 'Cookie')
        # Hop-by-hop headers belong to the ASGI server, not to the view.
        self.assertNotIn('Connection', response)
        self.assertNotIn('Upgrade', response)

    @override_settings(REALTIME_ENABLED=True)
    def test_database_connections_are_closed_before_the_stream_starts(self):
        self.client.force_login(self.user)
        sequence = []

        def close_connections():
            sequence.append('database_closed')

        async def probe():
            sequence.append('redis_probe')
            return True, ''

        async def stream(user_id):
            sequence.append(('stream', user_id))
            yield 'retry: 3000\n\n'

        with (
            mock.patch.object(views, '_close_sse_database_connections', close_connections),
            mock.patch.object(views, 'redis_is_reachable', probe),
            mock.patch.object(views, 'event_stream', stream),
        ):
            response = self.client.get(reverse('realtime:events'))
            drain(response, limit=1)

        self.assertEqual(
            sequence,
            ['database_closed', 'redis_probe', ('stream', self.user.pk)],
        )


@override_settings(REALTIME_ENABLED=True, REALTIME_CHANNEL_PREFIX='demo:realtime')
class SubscriptionTargetTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='sse_user', password='demo12345')
        cls.other = User.objects.create_user(username='sse_other', password='demo12345')

    def test_the_user_is_subscribed_to_their_own_channel_only(self):
        self.client.force_login(self.user)
        client = FakeAsyncRedis(script=[None])

        with _fake_transport(client):
            response = self.client.get(reverse('realtime:events'))
            drain(response, limit=2)

        self.assertEqual(
            client.pubsub_instance.subscribed, [f'demo:realtime:user:{self.user.pk}']
        )

    def test_a_client_supplied_user_id_is_ignored(self):
        self.client.force_login(self.user)
        client = FakeAsyncRedis(script=[None])

        with _fake_transport(client):
            response = self.client.get(
                reverse('realtime:events'),
                {
                    'user_id': self.other.pk,
                    'user': self.other.pk,
                    'target': f'user:{self.other.pk}',
                    'channel': 'demo:realtime:user:999',
                },
            )
            drain(response, limit=2)

        self.assertEqual(
            client.pubsub_instance.subscribed, [f'demo:realtime:user:{self.user.pk}']
        )
        self.assertNotIn(
            f'demo:realtime:user:{self.other.pk}', client.pubsub_instance.subscribed
        )

    def test_no_act_channel_is_subscribed(self):
        self.client.force_login(self.user)
        client = FakeAsyncRedis(script=[None])

        with _fake_transport(client):
            response = self.client.get(reverse('realtime:events'))
            drain(response, limit=2)

        self.assertFalse(
            any(':act:' in channel for channel in client.pubsub_instance.subscribed)
        )

    def test_a_valid_personal_event_reaches_the_stream(self):
        self.client.force_login(self.user)
        event = build_event()
        channel = user_channel(self.user.pk)
        client = FakeAsyncRedis(script=[message(channel, event.as_compact_json())])

        with _fake_transport(client):
            response = self.client.get(reverse('realtime:events'))
            frames = drain(response, limit=2)

        self.assertEqual(frames[0], 'retry: 3000\n\n')
        self.assertIn(f'id: {event.event_id}', frames[1])
        self.assertIn('event: notification.created', frames[1])
        payload = json.loads(frames[1].split('data: ', 1)[1].strip())
        self.assertEqual(payload, event.as_dict())

    def test_another_users_event_never_reaches_this_stream(self):
        self.client.force_login(self.user)
        foreign = build_event(resource_id=99)
        # Delivered on the other user's channel: this subscriber never sees it,
        # so the stream only produces its heartbeat.
        client = FakeAsyncRedis(
            script=[None, message(user_channel(self.other.pk), foreign.as_compact_json())]
        )

        with _fake_transport(client):
            response = self.client.get(reverse('realtime:events'))
            frames = drain(response, limit=2)

        self.assertEqual(
            client.pubsub_instance.subscribed, [f'demo:realtime:user:{self.user.pk}']
        )
        self.assertEqual(frames[1], ': heartbeat\n\n')
        self.assertNotIn(str(foreign.event_id), ''.join(frames))


def _probe_with(client):
    async def probe(*, client_factory=None):
        return await sse.redis_is_reachable(client_factory=lambda: client)

    return probe


def _fake_transport(client):
    """Patch both the preflight probe and the stream onto one fake client."""

    async def probe():
        return True, ''

    def stream(user_id, **kwargs):
        return sse.event_stream(user_id, client_factory=lambda: client, max_messages=1)

    return mock.patch.multiple(
        views, redis_is_reachable=probe, event_stream=stream
    )
