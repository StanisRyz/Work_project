from django.test import SimpleTestCase, override_settings

from realtime.channels import (
    RealtimeChannelError,
    act_channel,
    channel_for_target,
    channels_for_targets,
    diagnostic_channel,
    normalize_channel_prefix,
    user_channel,
)
from realtime.targets import act_target, user_target


class ChannelPrefixTests(SimpleTestCase):
    def test_the_configured_prefix_is_used(self):
        with override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime'):
            self.assertEqual(normalize_channel_prefix(), 'demo:realtime')

    def test_a_trailing_separator_is_removed(self):
        self.assertEqual(normalize_channel_prefix('demo:realtime:'), 'demo:realtime')

    def test_an_empty_prefix_is_refused(self):
        for invalid in ('', '   ', ':', None):
            with self.subTest(prefix=invalid):
                with override_settings(REALTIME_CHANNEL_PREFIX=invalid):
                    with self.assertRaises(RealtimeChannelError):
                        normalize_channel_prefix()

    def test_control_characters_and_whitespace_are_refused(self):
        for invalid in ('demo realtime', 'demo\nrealtime', 'demo\x00realtime', 'demo\trealtime'):
            with self.subTest(prefix=invalid):
                with self.assertRaisesMessage(RealtimeChannelError, 'недопустимые символы'):
                    normalize_channel_prefix(invalid)

    def test_glob_and_exotic_characters_are_refused(self):
        for invalid in ('demo*', 'demo?', 'demo[1]', 'демо', 'a' * 65):
            with self.subTest(prefix=invalid):
                with self.assertRaises(RealtimeChannelError):
                    normalize_channel_prefix(invalid)


class ChannelForTargetTests(SimpleTestCase):
    @override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
    def test_a_channel_is_prefix_plus_target_key(self):
        self.assertEqual(channel_for_target(user_target(7)), 'demo:realtime:user:7')
        self.assertEqual(channel_for_target(act_target(3)), 'demo:realtime:act:3')

    @override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
    def test_user_and_act_helpers_match_the_generic_builder(self):
        self.assertEqual(user_channel(7), 'demo:realtime:user:7')
        self.assertEqual(act_channel(3), 'demo:realtime:act:3')

    def test_only_a_real_target_is_accepted(self):
        # A client-supplied string must never become a channel name.
        for invalid in ('user:7', 'demo:realtime:user:7', 7, None, {'kind': 'user'}):
            with self.subTest(value=invalid):
                with self.assertRaisesMessage(RealtimeChannelError, 'только из RealtimeTarget'):
                    channel_for_target(invalid)

    def test_a_user_channel_needs_an_identifier(self):
        with self.assertRaises(RealtimeChannelError):
            user_channel(None)

    @override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
    def test_duplicate_targets_produce_one_channel(self):
        channels = channels_for_targets(
            [user_target(7), user_target(7), act_target(3)]
        )

        self.assertEqual(channels, ('demo:realtime:user:7', 'demo:realtime:act:3'))

    def test_an_explicit_prefix_overrides_the_setting(self):
        with override_settings(REALTIME_CHANNEL_PREFIX='ignored'):
            self.assertEqual(channel_for_target(user_target(1), 'chosen'), 'chosen:user:1')


class DiagnosticChannelTests(SimpleTestCase):
    @override_settings(REALTIME_CHANNEL_PREFIX='demo:realtime')
    def test_a_diagnostic_channel_lives_outside_the_user_namespace(self):
        channel = diagnostic_channel('a' * 32)

        self.assertEqual(channel, 'demo:realtime:diagnostic:' + 'a' * 32)
        self.assertNotIn(':user:', channel)
        self.assertNotIn(':act:', channel)

    def test_an_unsafe_token_is_refused(self):
        for invalid in ('', 'short', 'token with space', 'токен' * 3, 'a' * 65, None):
            with self.subTest(token=invalid):
                with self.assertRaisesMessage(RealtimeChannelError, 'token'):
                    diagnostic_channel(invalid)
