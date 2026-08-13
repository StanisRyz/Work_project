"""Server side: act.created, connection lifetime, checks and settings."""

import asyncio
import logging
from datetime import timedelta

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.checks import Error, Warning as CheckWarning
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts.models import UserProfile
from acts.models import Act
from realtime.checks import check_realtime_configuration
from realtime.events import RealtimeEventType
from realtime.sse import event_stream
from realtime.testing import capture_realtime_events

from .base import RealtimeFixtureMixin, target_keys
from .fakes import FakeAsyncRedis, message


class ActCreatedEventTests(RealtimeFixtureMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.setUpRealtimeData()
        cls.manager = cls.make_user('rt_manager', UserProfile.Role.MANAGER)
        cls.admin = cls.make_user('rt_admin', UserProfile.Role.ADMIN)

    def _create_act(self):
        return Act.objects.create(
            number='АОК-2026-00007',
            created_by=self.otk_user,
            party_number='P-RT5',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status_created,
            description='Описание',
        )

    def test_the_event_is_published_after_commit(self):
        from realtime.emitters import emit_act_created

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    act = self._create_act()
                    emit_act_created(act)

        events = publisher.events_of_type(RealtimeEventType.ACT_CREATED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].resource_type, 'act')
        self.assertEqual(events[0].resource_id, act.pk)
        self.assertEqual(events[0].data['status_code'], 'CREATED_OTK')
        self.assertEqual(events[0].data['author_id'], self.otk_user.pk)

    def test_a_rollback_publishes_nothing(self):
        from realtime.emitters import emit_act_created

        class Rollback(Exception):
            pass

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with self.assertRaises(Rollback):
                    with transaction.atomic():
                        act = self._create_act()
                        emit_act_created(act)
                        raise Rollback

        self.assertEqual(publisher.published, [])
        self.assertFalse(Act.objects.filter(party_number='P-RT5').exists())

    def test_the_payload_carries_no_business_data(self):
        from realtime.emitters import emit_act_created

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    act = self._create_act()
                    emit_act_created(act)

        payload = publisher.events[0].as_json()
        self.assertNotIn(act.number, payload)
        self.assertNotIn('Катушка', payload)
        self.assertNotIn('P-RT5', payload)

    def test_recipients_are_the_author_and_full_access_users(self):
        from realtime.emitters import emit_act_created

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    act = self._create_act()
                    emit_act_created(act)

        _event, targets = publisher.published[0]
        keys = target_keys(targets)
        self.assertIn(f'user:{self.otk_user.pk}', keys)
        self.assertIn(f'user:{self.manager.pk}', keys)
        self.assertIn(f'user:{self.admin.pk}', keys)
        self.assertIn(f'act:{act.pk}', keys)

    def test_ko_and_to_are_not_told_about_a_created_otk_act(self):
        from realtime.emitters import emit_act_created

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    act = self._create_act()
                    emit_act_created(act)

        keys = target_keys(publisher.published[0][1])
        # Global read access does not turn unrelated users into event recipients;
        # the periodic revision sync still refreshes their global registry.
        self.assertNotIn(f'user:{self.ko_user.pk}', keys)
        self.assertNotIn(f'user:{self.to_user.pk}', keys)
        self.assertNotIn(f'user:{self.outsider.pk}', keys)

    def test_inactive_users_and_profiles_are_excluded(self):
        from realtime.emitters import emit_act_created

        inactive_manager = self.make_user('rt_manager_off', UserProfile.Role.MANAGER)
        inactive_manager.is_active = False
        inactive_manager.save(update_fields=['is_active'])
        disabled_profile = self.make_user('rt_manager_profile_off', UserProfile.Role.MANAGER)
        disabled_profile.userprofile.is_active = False
        disabled_profile.userprofile.save(update_fields=['is_active'])

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    emit_act_created(self._create_act())

        keys = target_keys(publisher.published[0][1])
        self.assertNotIn(f'user:{inactive_manager.pk}', keys)
        self.assertNotIn(f'user:{disabled_profile.pk}', keys)

    def test_targets_contain_no_duplicates(self):
        from realtime.emitters import emit_act_created

        # The author is also a full-access user: still exactly one target.
        author = self.make_user('rt_author_admin', UserProfile.Role.ADMIN)
        act = Act.objects.create(
            created_by=author,
            party_number='P-DUP',
            nomenclature='Катушка',
            operation=self.operation,
            defect_type=self.defect_type,
            status=self.status_created,
            description='Описание',
        )

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    emit_act_created(act)

        keys = target_keys(publisher.published[0][1])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys.count(f'user:{author.pk}'), 1)

    def test_creating_an_act_through_the_view_publishes_the_event(self):
        self.client.force_login(self.otk_user)
        payload = {
            'number_suffix': '34',
            'customer': 'Заказчик',
            'order_number': '100-1',
            'nomenclature': 'Катушка-А',
            'kd_designation': 'КД-100',
            'defects-TOTAL_FORMS': '1',
            'defects-INITIAL_FORMS': '0',
            'defects-MIN_NUM_FORMS': '1',
            'defects-MAX_NUM_FORMS': '1000',
            'defects-0-workshop': 'MP_SHOP',
            'defects-0-defect_type': self.defect_type.id,
            'defects-0-operation': self.operation.id,
            'defects-0-mp_type': 'OL',
            'defects-0-znp_number': '200-1',
            'defects-0-party_number': '100-100',
            'defects-0-checked_quantity': '100',
            'defects-0-nonconforming_quantity': '4',
            'defects-0-description': 'Описание дефекта',
            'defects-0-detected_at': self.today().isoformat(),
        }

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse('acts:create'), payload)

        self.assertEqual(response.status_code, 302, getattr(response, 'context', None))
        events = publisher.events_of_type(RealtimeEventType.ACT_CREATED)
        self.assertEqual(len(events), 1)

    def test_an_invalid_form_publishes_nothing(self):
        self.client.force_login(self.otk_user)

        with capture_realtime_events() as publisher:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse('acts:create'), {'party_number': ''})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(publisher.events_of_type(RealtimeEventType.ACT_CREATED), [])

    @staticmethod
    def today():
        from django.utils import timezone as django_timezone

        return django_timezone.localdate()


@override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime', REALTIME_HEARTBEAT_SECONDS=0.01)
class ConnectionLifetimeTests(TestCase):
    """The stream must end by itself so a session cannot be outlived."""

    def _collect(self, client, *, max_lifetime, limit=10):
        async def run():
            frames = []
            stream = event_stream(
                7, client_factory=lambda: client, max_lifetime=max_lifetime
            )
            async for frame in stream:
                frames.append(frame)
                if len(frames) > limit:
                    break
            await stream.aclose()
            return frames

        return async_to_sync(run)()

    def test_the_stream_ends_after_the_lifetime_limit(self):
        # Every `get_message` returns None, so only heartbeats are produced and
        # the loop keeps running until the deadline stops it.
        client = FakeAsyncRedis(script=[], honour_timeout=True)

        frames = self._collect(client, max_lifetime=0.05, limit=50)

        self.assertTrue(frames)
        self.assertLess(len(frames), 50, 'the lifetime must stop the loop')

    def test_reaching_the_limit_releases_the_subscription_and_client(self):
        client = FakeAsyncRedis(script=[], honour_timeout=True)

        self._collect(client, max_lifetime=0.05)

        self.assertEqual(client.pubsub_instance.unsubscribed, ['demo:realtime:user:7'])
        self.assertTrue(client.pubsub_instance.closed)
        self.assertTrue(client.closed)

    def test_a_normal_end_is_not_logged_as_an_error(self):
        client = FakeAsyncRedis(script=[], honour_timeout=True)

        with self.assertLogs('realtime', level=logging.INFO) as captured:
            self._collect(client, max_lifetime=0.05)

        joined = '\n'.join(captured.output)
        self.assertIn('realtime.connection_opened', joined)
        self.assertIn('realtime.connection_closed', joined)
        self.assertIn('reason=max_lifetime', joined)
        self.assertNotIn('ERROR', joined)

    def test_a_reconnect_gets_a_fresh_connection_id(self):
        first = FakeAsyncRedis(script=[], honour_timeout=True)
        second = FakeAsyncRedis(script=[], honour_timeout=True)

        with self.assertLogs('realtime', level=logging.INFO) as captured:
            self._collect(first, max_lifetime=0.05)
            self._collect(second, max_lifetime=0.05)

        opened = [line for line in captured.output if 'connection_opened' in line]
        self.assertEqual(len(opened), 2)
        self.assertNotEqual(opened[0].split('connection_id=')[1][:12],
                            opened[1].split('connection_id=')[1][:12])

    @override_settings(REALTIME_HEARTBEAT_SECONDS=25, REALTIME_MAX_CONNECTION_SECONDS=900)
    def test_the_heartbeat_is_shorter_than_the_lifetime(self):
        from django.conf import settings

        self.assertLess(
            settings.REALTIME_HEARTBEAT_SECONDS, settings.REALTIME_MAX_CONNECTION_SECONDS
        )

    def test_an_event_still_reaches_the_client_before_the_limit(self):
        from realtime.events import RealtimeEvent

        event = RealtimeEvent(
            event_type=RealtimeEventType.ACT_CREATED,
            resource_type='act',
            resource_id=3,
            data={'status_code': 'CREATED_OTK', 'author_id': 1},
        )
        client = FakeAsyncRedis(
            script=[message('demo:realtime:user:7', event.as_compact_json())]
        )

        frames = self._collect(client, max_lifetime=5, limit=2)

        self.assertIn('event: act.created', frames[1])


class RealtimeSystemCheckTests(TestCase):
    def _ids(self, **settings_kwargs):
        with override_settings(**settings_kwargs):
            return {issue.id: issue for issue in check_realtime_configuration(None)}

    def test_a_healthy_default_configuration_reports_nothing(self):
        self.assertEqual(self._ids(REALTIME_ENABLED=False), {})

    def test_enabled_realtime_with_the_noop_publisher_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.NoopRealtimePublisher',
        )

        self.assertIn('realtime.E001', issues)

    @override_settings(DEBUG=False)
    def test_severity_is_an_error_once_debug_is_off(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.NoopRealtimePublisher',
        )

        self.assertIsInstance(issues['realtime.E001'], Error)

    @override_settings(DEBUG=True)
    def test_severity_is_a_warning_while_developing(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.NoopRealtimePublisher',
        )

        self.assertIsInstance(issues['realtime.E001'], CheckWarning)

    def test_a_missing_redis_url_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_REDIS_URL='',
        )

        self.assertIn('realtime.E002', issues)

    def test_a_malformed_redis_url_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_REDIS_URL='not-a-url',
        )

        self.assertIn('realtime.E003', issues)

    def test_a_wrong_scheme_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_REDIS_URL='http://127.0.0.1:6379/0',
        )

        self.assertIn('realtime.E004', issues)

    def test_rediss_is_accepted(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_REDIS_URL='rediss://redis.internal:6379/0',
        )

        self.assertNotIn('realtime.E004', issues)

    def test_an_invalid_channel_prefix_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_CHANNEL_PREFIX='bad prefix',
        )

        self.assertIn('realtime.E005', issues)

    def test_a_missing_asgi_application_is_reported(self):
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            ASGI_APPLICATION='',
        )

        self.assertIn('realtime.E006', issues)

    def test_a_heartbeat_at_least_as_long_as_the_lifetime_is_reported(self):
        issues = self._ids(
            REALTIME_HEARTBEAT_SECONDS=900.0, REALTIME_MAX_CONNECTION_SECONDS=900.0
        )

        self.assertIn('realtime.E007', issues)

    def test_a_hidden_poll_faster_than_the_visible_one_is_reported(self):
        issues = self._ids(
            REALTIME_SYNC_POLL_SECONDS=60.0, REALTIME_SYNC_HIDDEN_POLL_SECONDS=30.0
        )

        self.assertIn('realtime.W001', issues)

    def test_a_leader_heartbeat_at_least_as_long_as_the_lease_is_reported(self):
        issues = self._ids(
            REALTIME_LEADER_LEASE_SECONDS=4.0, REALTIME_LEADER_HEARTBEAT_SECONDS=4.0
        )

        self.assertIn('realtime.E008', issues)

    def test_no_message_contains_redis_credentials(self):
        secret_url = 'redis://appuser:s3cr3t-redis-password@redis.internal:6379/2'
        issues = self._ids(
            REALTIME_ENABLED=True,
            REALTIME_PUBLISHER_BACKEND='realtime.backends.RedisRealtimePublisher',
            REALTIME_REDIS_URL=secret_url.replace('redis://', 'http://'),
        )

        text = '\n'.join(f'{issue.msg} {issue.hint}' for issue in issues.values())
        self.assertNotIn('s3cr3t-redis-password', text)
        self.assertNotIn('appuser', text)
        self.assertNotIn(secret_url, text)


class RealtimeSettingsTests(TestCase):
    def test_every_rt5_setting_has_a_sane_default(self):
        from django.conf import settings

        self.assertGreater(settings.REALTIME_DEGRADED_AFTER_SECONDS, 0)
        self.assertGreater(settings.REALTIME_SYNC_POLL_SECONDS, 0)
        self.assertGreaterEqual(
            settings.REALTIME_SYNC_HIDDEN_POLL_SECONDS, settings.REALTIME_SYNC_POLL_SECONDS
        )
        self.assertGreater(settings.REALTIME_MAX_CONNECTION_SECONDS, settings.REALTIME_HEARTBEAT_SECONDS)
        self.assertGreater(
            settings.REALTIME_LEADER_LEASE_SECONDS, settings.REALTIME_LEADER_HEARTBEAT_SECONDS
        )

    def test_an_out_of_range_value_is_refused(self):
        import os
        from importlib import reload

        from django.core.exceptions import ImproperlyConfigured

        os.environ['REALTIME_SYNC_POLL_SECONDS'] = '0'
        try:
            with self.assertRaises(ImproperlyConfigured):
                import ecosystem.settings as project_settings

                reload(project_settings)
        finally:
            os.environ.pop('REALTIME_SYNC_POLL_SECONDS', None)
            import ecosystem.settings as project_settings

            reload(project_settings)

    def test_the_client_config_exposes_only_safe_numbers(self):
        user = User.objects.create_user(username='rt5_config', password='demo12345')
        self.client.force_login(user)

        with override_settings(REALTIME_ENABLED=True):
            content = self.client.get(reverse('acts:list')).content.decode()

        self.assertIn('data-sync-url="/realtime/sync/"', content)
        self.assertIn('data-degraded-after-seconds=', content)
        self.assertIn('data-sync-poll-seconds=', content)
        self.assertIn('data-leader-lease-seconds=', content)
        self.assertNotIn('redis://', content)
        self.assertNotIn('quality-ecosystem:realtime', content)
