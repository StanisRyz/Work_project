from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

from realtime.management.commands import check_realtime_transport as command_module

from .fakes import FakeSyncRedis, connection_error, timeout_error


SECRET_URL = 'redis://appuser:s3cr3t-redis-password@redis.internal:6379/2'


class EchoingRedis(FakeSyncRedis):
    """Publishes straight back into its own subscribers, like a real server."""


class SilentRedis(FakeSyncRedis):
    """Accepts the publish but never delivers it: reproduces a timeout."""

    def publish(self, channel, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((channel, message))
        return 0


class WrongTokenRedis(FakeSyncRedis):
    """Delivers a different payload than the one published."""

    def publish(self, channel, message):
        self.published.append((channel, message))
        for pubsub in self.pubsubs:
            pubsub.deliver(channel, b'a-completely-different-token')
        return 1


@override_settings(
    REALTIME_CHANNEL_PREFIX='demo:realtime',
    REALTIME_REDIS_URL='redis://127.0.0.1:6379/0',
)
class DiagnosticCommandTests(SimpleTestCase):
    def _run(self, client, **options):
        buffer = StringIO()
        with mock.patch.object(command_module, 'sync_client', return_value=client):
            call_command('check_realtime_transport', stdout=buffer, **options)
        return buffer.getvalue()

    def _run_expecting_failure(self, client, **options):
        buffer = StringIO()
        with mock.patch.object(command_module, 'sync_client', return_value=client):
            with self.assertRaises(CommandError) as ctx:
                call_command('check_realtime_transport', stdout=buffer, **options)
        return buffer.getvalue(), str(ctx.exception)

    def test_a_successful_round_trip_reports_settings_ping_and_timing(self):
        output = self._run(EchoingRedis())

        self.assertIn('Redis                    — redis://127.0.0.1:6379/0', output)
        self.assertIn('Префикс каналов          — demo:realtime', output)
        self.assertIn('PING — ok', output)
        self.assertIn('Round trip:', output)
        self.assertIn('Транспорт real-time доступен', output)

    def test_the_diagnostic_channel_is_unique_and_outside_the_user_namespace(self):
        client = EchoingRedis()

        self._run(client)

        channel = client.published[0][0]
        self.assertTrue(channel.startswith('demo:realtime:diagnostic:'))
        self.assertNotIn(':user:', channel)
        self.assertNotIn(':act:', channel)
        self.assertEqual(client.pubsubs[0].subscribed, [channel])

    def test_two_runs_use_different_channels(self):
        first, second = EchoingRedis(), EchoingRedis()

        self._run(first)
        self._run(second)

        self.assertNotEqual(first.published[0][0], second.published[0][0])

    def test_resources_are_released_after_a_successful_run(self):
        client = EchoingRedis()

        self._run(client)

        pubsub = client.pubsubs[0]
        self.assertEqual(pubsub.unsubscribed, pubsub.subscribed)
        self.assertTrue(pubsub.closed)
        self.assertTrue(client.closed)

    def test_a_connection_failure_is_reported_as_a_command_error(self):
        client = FakeSyncRedis(ping_error=connection_error())

        _output, error = self._run_expecting_failure(client)

        self.assertIn('PING', error)
        self.assertIn('ConnectionError', error)

    def test_a_publish_failure_is_reported_as_a_command_error(self):
        client = FakeSyncRedis(publish_error=connection_error())

        _output, error = self._run_expecting_failure(client)

        self.assertIn('опубликовать', error)

    def test_a_missing_message_times_out_with_a_clear_error(self):
        _output, error = self._run_expecting_failure(SilentRedis(), timeout=0.2)

        self.assertIn('не получено', error)
        self.assertIn('redis://127.0.0.1:6379/0', error)

    def test_a_token_mismatch_is_reported(self):
        _output, error = self._run_expecting_failure(WrongTokenRedis(), timeout=1)

        self.assertIn('не совпадающее с отправленным token', error)

    def test_a_non_positive_timeout_is_refused(self):
        with self.assertRaisesMessage(CommandError, '--timeout'):
            call_command('check_realtime_transport', '--timeout', '0', stdout=StringIO())

    def test_resources_are_released_even_when_the_round_trip_fails(self):
        client = SilentRedis()

        self._run_expecting_failure(client, timeout=0.2)

        pubsub = client.pubsubs[0]
        self.assertTrue(pubsub.closed)
        self.assertTrue(client.closed)

    def test_a_disabled_realtime_configuration_still_diagnoses_the_transport(self):
        with override_settings(REALTIME_ENABLED=False):
            output = self._run(EchoingRedis())

        self.assertIn('REALTIME_ENABLED         — False', output)
        self.assertIn('события не публикуются', output)
        self.assertIn('Транспорт real-time доступен', output)


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime', REALTIME_REDIS_URL=SECRET_URL)
class DiagnosticCredentialTests(SimpleTestCase):
    def _assert_no_credentials(self, *texts):
        joined = '\n'.join(texts)
        self.assertNotIn('s3cr3t-redis-password', joined)
        self.assertNotIn('appuser', joined)
        self.assertNotIn(SECRET_URL, joined)

    def test_successful_output_shows_the_host_without_credentials(self):
        buffer = StringIO()
        with mock.patch.object(command_module, 'sync_client', return_value=EchoingRedis()):
            call_command('check_realtime_transport', stdout=buffer)

        output = buffer.getvalue()
        self._assert_no_credentials(output)
        self.assertIn('redis://redis.internal:6379/2', output)

    def test_failure_output_and_error_contain_no_credentials(self):
        client = FakeSyncRedis(ping_error=connection_error(f'Cannot reach {SECRET_URL}'))
        buffer = StringIO()

        with mock.patch.object(command_module, 'sync_client', return_value=client):
            with self.assertRaises(CommandError) as ctx:
                call_command('check_realtime_transport', stdout=buffer)

        self._assert_no_credentials(buffer.getvalue(), str(ctx.exception))
        self.assertIn('redis.internal', str(ctx.exception))

    def test_a_timeout_error_contains_no_credentials(self):
        buffer = StringIO()

        with mock.patch.object(command_module, 'sync_client', return_value=SilentRedis()):
            with self.assertRaises(CommandError) as ctx:
                call_command('check_realtime_transport', '--timeout', '0.2', stdout=buffer)

        self._assert_no_credentials(buffer.getvalue(), str(ctx.exception))

    def test_a_socket_timeout_error_contains_no_credentials(self):
        client = FakeSyncRedis(publish_error=timeout_error(f'Timeout talking to {SECRET_URL}'))
        buffer = StringIO()

        with mock.patch.object(command_module, 'sync_client', return_value=client):
            with self.assertRaises(CommandError) as ctx:
                call_command('check_realtime_transport', stdout=buffer)

        self._assert_no_credentials(buffer.getvalue(), str(ctx.exception))
