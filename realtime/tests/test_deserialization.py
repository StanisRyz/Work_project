import json
import uuid

from django.test import SimpleTestCase

from realtime.events import (
    SCHEMA_VERSION,
    RealtimeEvent,
    RealtimeEventError,
    RealtimeEventType,
)


def build_event(**overrides):
    payload = {
        'event_type': RealtimeEventType.NOTIFICATION_CREATED,
        'resource_type': 'notification',
        'resource_id': 11,
        'data': {'act_id': 3, 'recipient_id': 5, 'actor_id': None},
    }
    payload.update(overrides)
    return RealtimeEvent(**payload)


class RoundTripTests(SimpleTestCase):
    def test_a_dict_round_trip_preserves_every_field(self):
        original = build_event()

        restored = RealtimeEvent.from_dict(original.as_dict())

        self.assertEqual(restored.schema_version, original.schema_version)
        self.assertEqual(restored.event_id, original.event_id)
        self.assertEqual(restored.event_type, original.event_type)
        self.assertEqual(restored.occurred_at, original.occurred_at)
        self.assertEqual(restored.resource_type, original.resource_type)
        self.assertEqual(restored.resource_id, original.resource_id)
        self.assertEqual(restored.data, original.data)

    def test_a_json_round_trip_reproduces_the_same_payload(self):
        original = build_event()

        restored = RealtimeEvent.from_json(original.as_json())

        self.assertEqual(restored.as_dict(), original.as_dict())
        self.assertEqual(restored.as_json(), original.as_json())

    def test_a_compact_json_round_trip_works_the_same(self):
        original = build_event()

        restored = RealtimeEvent.from_json(original.as_compact_json())

        self.assertEqual(restored.as_dict(), original.as_dict())

    def test_bytes_are_accepted_as_received_from_redis(self):
        original = build_event()

        restored = RealtimeEvent.from_json(original.as_compact_json().encode('utf-8'))

        self.assertEqual(restored.event_id, original.event_id)

    def test_unicode_survives_the_round_trip(self):
        original = build_event(data={'note': 'Акт АОК-2026-001 — проверка'})

        restored = RealtimeEvent.from_json(original.as_compact_json())

        self.assertEqual(restored.data['note'], 'Акт АОК-2026-001 — проверка')

    def test_the_deterministic_serialization_is_unchanged(self):
        event = build_event()

        # `as_json` keeps the RT-1 wire format: sorted keys, indented-free JSON.
        self.assertEqual(json.loads(event.as_json()), event.as_dict())
        self.assertEqual(event.as_json(), event.as_json())
        # `as_compact_json` is the same document without padding.
        self.assertEqual(json.loads(event.as_compact_json()), event.as_dict())
        self.assertNotIn(', ', event.as_compact_json())

    def test_byte_size_matches_the_compact_payload(self):
        event = build_event()

        self.assertEqual(event.byte_size(), len(event.as_compact_json().encode('utf-8')))


class DeserializationValidationTests(SimpleTestCase):
    def _payload(self, **overrides):
        payload = build_event().as_dict()
        payload.update(overrides)
        return payload

    def test_a_non_object_payload_is_rejected(self):
        for invalid in ([], 'text', 7, None):
            with self.subTest(payload=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'объектом JSON'):
                    RealtimeEvent.from_dict(invalid)

    def test_an_unknown_schema_version_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'Неподдерживаемая версия схемы'):
            RealtimeEvent.from_dict(self._payload(schema_version=SCHEMA_VERSION + 1))

    def test_a_zero_schema_version_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'schema_version начинается с 1'):
            RealtimeEvent.from_dict(self._payload(schema_version=0))

    def test_an_unknown_event_type_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'Неизвестный тип события'):
            RealtimeEvent.from_dict(self._payload(event_type='act.exploded'))

    def test_a_malformed_uuid_is_rejected(self):
        for invalid in ('not-a-uuid', '', '1234'):
            with self.subTest(event_id=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'Некорректный event_id'):
                    RealtimeEvent.from_dict(self._payload(event_id=invalid))

    def test_a_non_string_event_id_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'строкой UUID'):
            RealtimeEvent.from_dict(self._payload(event_id=7))

    def test_a_malformed_datetime_is_rejected(self):
        for invalid in ('yesterday', '2026-13-45T99:99:99', ''):
            with self.subTest(occurred_at=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'Некорректный occurred_at'):
                    RealtimeEvent.from_dict(self._payload(occurred_at=invalid))

    def test_a_naive_datetime_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'timezone'):
            RealtimeEvent.from_dict(self._payload(occurred_at='2026-08-04T12:00:00'))

    def test_an_unknown_resource_type_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'Неизвестный resource_type'):
            RealtimeEvent.from_dict(self._payload(resource_type='protocol'))

    def test_an_invalid_resource_id_is_rejected(self):
        for invalid in (0, -3, '11', None):
            with self.subTest(resource_id=invalid):
                with self.assertRaises(RealtimeEventError):
                    RealtimeEvent.from_dict(self._payload(resource_id=invalid))

    def test_unsafe_data_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'data должен быть словарём'):
            RealtimeEvent.from_dict(self._payload(data=['a', 'b']))

    def test_a_missing_field_is_reported(self):
        payload = self._payload()
        payload.pop('resource_id')

        with self.assertRaisesMessage(RealtimeEventError, 'отсутствуют поля: resource_id'):
            RealtimeEvent.from_dict(payload)

    def test_targets_are_not_accepted_from_the_wire(self):
        payload = self._payload()
        payload['targets'] = ['user:7']

        with self.assertRaisesMessage(RealtimeEventError, 'посторонние поля: targets'):
            RealtimeEvent.from_dict(payload)

    def test_other_transport_fields_are_not_accepted(self):
        payload = self._payload()
        payload['channel'] = 'demo:realtime:user:7'

        with self.assertRaisesMessage(RealtimeEventError, 'посторонние поля: channel'):
            RealtimeEvent.from_dict(payload)

    def test_invalid_json_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'не является корректным JSON'):
            RealtimeEvent.from_json('{not json')

    def test_invalid_utf8_bytes_are_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'корректным UTF-8'):
            RealtimeEvent.from_json(b'\xff\xfe\x00')

    def test_an_unsupported_input_type_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'Ожидались строка или байты'):
            RealtimeEvent.from_json({'event_id': str(uuid.uuid4())})


class SizeLimitTests(SimpleTestCase):
    def test_an_oversized_json_string_is_rejected(self):
        event = build_event(data={'note': 'x' * 2000})

        with self.assertRaisesMessage(RealtimeEventError, 'превышает допустимый размер'):
            RealtimeEvent.from_json(event.as_compact_json(), max_bytes=256)

    def test_an_oversized_byte_payload_is_rejected_before_parsing(self):
        event = build_event(data={'note': 'x' * 2000})

        with self.assertRaisesMessage(RealtimeEventError, 'Сообщение превышает'):
            RealtimeEvent.from_json(event.as_compact_json().encode('utf-8'), max_bytes=256)

    def test_an_oversized_dict_is_rejected(self):
        event = build_event(data={'note': 'x' * 2000})

        with self.assertRaisesMessage(RealtimeEventError, 'Событие превышает'):
            RealtimeEvent.from_dict(event.as_dict(), max_bytes=256)

    def test_a_payload_within_the_limit_is_accepted(self):
        event = build_event()

        restored = RealtimeEvent.from_json(event.as_compact_json(), max_bytes=16384)

        self.assertEqual(restored.event_id, event.event_id)

    def test_multibyte_characters_count_as_their_utf8_size(self):
        event = build_event(data={'note': 'я' * 200})

        self.assertGreater(event.byte_size(), 400)
        with self.assertRaises(RealtimeEventError):
            RealtimeEvent.from_json(event.as_compact_json(), max_bytes=300)
