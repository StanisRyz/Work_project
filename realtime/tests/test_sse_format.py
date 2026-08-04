import json

from django.test import SimpleTestCase, override_settings

from realtime.events import RealtimeEvent, RealtimeEventType
from realtime.sse import format_event, format_heartbeat, format_retry


def build_event(**overrides):
    payload = {
        'event_type': RealtimeEventType.COMMENT_CREATED,
        'resource_type': 'comment',
        'resource_id': 42,
        'data': {'act_id': 7, 'author_id': 5},
    }
    payload.update(overrides)
    return RealtimeEvent(**payload)


class RetryFrameTests(SimpleTestCase):
    @override_settings(REALTIME_RECONNECT_DELAY_MS=3000)
    def test_the_retry_frame_uses_the_configured_delay(self):
        self.assertEqual(format_retry(), 'retry: 3000\n\n')

    def test_an_explicit_delay_overrides_the_setting(self):
        with override_settings(REALTIME_RECONNECT_DELAY_MS=3000):
            self.assertEqual(format_retry(750), 'retry: 750\n\n')

    def test_the_retry_frame_ends_with_two_newlines(self):
        self.assertTrue(format_retry().endswith('\n\n'))


class EventFrameTests(SimpleTestCase):
    def test_the_frame_carries_id_event_and_data(self):
        event = build_event()

        frame = format_event(event)
        lines = frame.rstrip('\n').split('\n')

        self.assertEqual(lines[0], f'id: {event.event_id}')
        self.assertEqual(lines[1], 'event: comment.created')
        self.assertTrue(lines[2].startswith('data: '))
        self.assertEqual(len(lines), 3)

    def test_the_data_line_is_compact_single_line_json(self):
        event = build_event()

        frame = format_event(event)
        data_line = [line for line in frame.split('\n') if line.startswith('data: ')][0]
        payload = json.loads(data_line[len('data: '):])

        self.assertEqual(payload, event.as_dict())
        self.assertNotIn('\n', data_line)

    def test_the_frame_ends_with_exactly_two_newlines(self):
        frame = format_event(build_event())

        self.assertTrue(frame.endswith('\n\n'))
        self.assertFalse(frame.endswith('\n\n\n'))

    def test_the_event_name_is_the_enum_value(self):
        for event_type in RealtimeEventType:
            with self.subTest(event_type=event_type):
                event = build_event(
                    event_type=event_type, resource_type='act', resource_id=1, data={}
                )
                self.assertIn(f'event: {event_type.value}\n', format_event(event))

    def test_unicode_is_preserved_and_encodes_as_utf8(self):
        event = build_event(data={'note': 'Акт АОК-2026-001 — проверка'})

        frame = format_event(event)

        self.assertIn('Акт АОК-2026-001 — проверка', frame)
        self.assertIn('Акт', frame.encode('utf-8').decode('utf-8'))

    def test_the_frame_contains_neither_targets_nor_a_channel_name(self):
        event = build_event()

        frame = format_event(event)

        self.assertNotIn('targets', frame)
        self.assertNotIn('user:', frame)
        self.assertNotIn('quality-ecosystem:realtime', frame)


class HeartbeatFrameTests(SimpleTestCase):
    def test_the_heartbeat_is_an_sse_comment(self):
        self.assertEqual(format_heartbeat(), ': heartbeat\n\n')

    def test_the_heartbeat_ends_with_two_newlines(self):
        self.assertTrue(format_heartbeat().endswith('\n\n'))

    def test_newlines_in_a_comment_cannot_break_the_frame(self):
        frame = format_heartbeat('two\nlines\r\nhere')

        self.assertEqual(frame.count('\n'), 2)
        self.assertTrue(frame.startswith(': '))
