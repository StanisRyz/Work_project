import json
import uuid
from datetime import datetime, timezone as dt_timezone

from django.test import SimpleTestCase
from django.utils import timezone

from realtime.events import (
    SCHEMA_VERSION,
    RealtimeEvent,
    RealtimeEventError,
    RealtimeEventType,
)


def build_event(**overrides):
    payload = {
        'event_type': RealtimeEventType.ACT_UPDATED,
        'resource_type': 'act',
        'resource_id': 7,
        'data': {'status_code': 'KO_REVIEW'},
    }
    payload.update(overrides)
    return RealtimeEvent(**payload)


class EventTypeContractTests(SimpleTestCase):
    def test_event_type_values_are_stable(self):
        # These strings are the wire contract: changing one breaks every client.
        self.assertEqual(
            {member.name: member.value for member in RealtimeEventType},
            {
                'NOTIFICATION_CREATED': 'notification.created',
                'NOTIFICATION_READ': 'notification.read',
                'TASK_CREATED': 'task.created',
                'TASK_UPDATED': 'task.updated',
                'TASK_COMPLETED': 'task.completed',
                'ACT_CREATED': 'act.created',
                'ACT_UPDATED': 'act.updated',
                'ACT_STATUS_CHANGED': 'act.status_changed',
                'COMMENT_CREATED': 'comment.created',
                'WORKUP_CREATED': 'workup.created',
                'WORKUP_UPDATED': 'workup.updated',
                'WORKUP_DELETED': 'workup.deleted',
            },
        )

    def test_event_type_is_a_plain_string_on_the_wire(self):
        self.assertEqual(build_event().as_dict()['event_type'], 'act.updated')

    def test_a_raw_string_is_coerced_to_the_enum(self):
        event = build_event(event_type='act.status_changed')
        self.assertIs(event.event_type, RealtimeEventType.ACT_STATUS_CHANGED)

    def test_an_unknown_event_type_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'Неизвестный тип события'):
            build_event(event_type='act.exploded')


class EventSerializationTests(SimpleTestCase):
    def test_payload_contains_exactly_the_contract_fields(self):
        payload = build_event().as_dict()

        self.assertEqual(
            sorted(payload),
            [
                'data',
                'event_id',
                'event_type',
                'occurred_at',
                'resource_id',
                'resource_type',
                'schema_version',
            ],
        )
        self.assertEqual(payload['schema_version'], SCHEMA_VERSION)
        self.assertEqual(payload['resource_type'], 'act')
        self.assertEqual(payload['resource_id'], 7)
        self.assertEqual(payload['data'], {'status_code': 'KO_REVIEW'})

    def test_serialization_is_deterministic_and_json_round_trips(self):
        event = build_event(data={'b': 2, 'a': [1, 2, {'c': None}]})

        first = event.as_json()
        second = event.as_json()

        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), event.as_dict())
        # Sorted keys make two serialisations of equal data byte-identical.
        self.assertLess(first.index('"data"'), first.index('"event_id"'))

    def test_targets_are_never_part_of_the_public_payload(self):
        event = build_event()

        self.assertNotIn('targets', event.as_dict())
        self.assertNotIn('targets', event.as_json())
        self.assertFalse(hasattr(event, 'targets'))

    def test_every_event_gets_its_own_uuid(self):
        identifiers = {build_event().event_id for _ in range(50)}

        self.assertEqual(len(identifiers), 50)
        for identifier in identifiers:
            self.assertIsInstance(identifier, uuid.UUID)

    def test_occurred_at_is_timezone_aware_by_default(self):
        event = build_event()

        self.assertTrue(timezone.is_aware(event.occurred_at))
        self.assertIn('+', event.as_dict()['occurred_at'].replace('+00:00', '+00:00'))

    def test_the_event_is_immutable_and_copies_its_data(self):
        source = {'status_code': 'KO_REVIEW'}
        event = build_event(data=source)

        source['status_code'] = 'ПОДМЕНА'
        event.as_dict()['data']['status_code'] = 'ПОДМЕНА'

        self.assertEqual(event.data['status_code'], 'KO_REVIEW')
        with self.assertRaises(Exception):
            event.resource_id = 99

    def test_log_context_carries_no_payload(self):
        context = build_event(data={'status_code': 'KO_REVIEW'}).log_context()

        self.assertEqual(
            sorted(context), ['event_id', 'event_type', 'resource_id', 'resource_type']
        )
        self.assertNotIn('KO_REVIEW', json.dumps(context))


class EventValidationTests(SimpleTestCase):
    def test_schema_version_starts_at_one(self):
        for invalid in (0, -1):
            with self.subTest(schema_version=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'schema_version начинается с 1'):
                    build_event(schema_version=invalid)

    def test_schema_version_must_be_an_integer(self):
        with self.assertRaisesMessage(RealtimeEventError, 'целым числом'):
            build_event(schema_version='1')

    def test_event_id_must_be_a_uuid(self):
        with self.assertRaisesMessage(RealtimeEventError, 'event_id должен быть UUID'):
            build_event(event_id='7c9f6f0a')

    def test_naive_occurred_at_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'timezone'):
            build_event(occurred_at=datetime(2026, 8, 4, 12, 0, 0))

    def test_aware_occurred_at_is_accepted(self):
        moment = datetime(2026, 8, 4, 12, 0, 0, tzinfo=dt_timezone.utc)

        self.assertEqual(build_event(occurred_at=moment).occurred_at, moment)

    def test_resource_type_cannot_be_empty_or_unknown(self):
        for invalid in ('', '   ', 'protocol'):
            with self.subTest(resource_type=invalid):
                with self.assertRaises(RealtimeEventError):
                    build_event(resource_type=invalid)

    def test_resource_id_must_be_a_positive_integer(self):
        for invalid in (0, -1, -100):
            with self.subTest(resource_id=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'положительным'):
                    build_event(resource_id=invalid)
        for invalid in ('7', 7.0, None, True):
            with self.subTest(resource_id=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'целым числом'):
                    build_event(resource_id=invalid)

    def test_data_must_be_a_dict(self):
        with self.assertRaisesMessage(RealtimeEventError, 'data должен быть словарём'):
            build_event(data=[1, 2, 3])

    def test_unsupported_data_values_are_rejected(self):
        unsupported = {
            'datetime': timezone.now(),
            'set': {1, 2},
            'bytes': b'binary',
            'object': object(),
        }
        for name, value in unsupported.items():
            with self.subTest(value=name):
                with self.assertRaisesMessage(RealtimeEventError, 'не является JSON-безопасным'):
                    build_event(data={'value': value})

    def test_non_finite_floats_are_rejected(self):
        for invalid in (float('nan'), float('inf')):
            with self.subTest(value=invalid):
                with self.assertRaisesMessage(RealtimeEventError, 'NaN'):
                    build_event(data={'value': invalid})

    def test_nested_json_safe_values_are_accepted(self):
        event = build_event(data={'ids': [1, 2, 3], 'flags': {'read': True, 'note': None}})

        self.assertEqual(event.data['ids'], [1, 2, 3])
        self.assertEqual(json.loads(event.as_json())['data']['flags']['read'], True)

    def test_unsupported_values_nested_in_a_list_are_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'не является JSON-безопасным'):
            build_event(data={'ids': [1, timezone.now()]})

    def test_non_string_data_keys_are_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'непустыми строками'):
            build_event(data={1: 'value'})

    def test_excessively_nested_data_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'вложенность'):
            build_event(data={'a': {'b': {'c': {'d': {'e': 1}}}}})

    def test_an_oversized_payload_is_rejected(self):
        with self.assertRaisesMessage(RealtimeEventError, 'слишком много ключей'):
            build_event(data={f'key_{index}': index for index in range(50)})
