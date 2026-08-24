"""The immutable real-time event contract.

An event says *what changed*, never *what the new content is*: it carries
identifiers and safe technical metadata only. PostgreSQL and the ordinary
Django endpoints remain the single source of truth — a client that receives an
event is expected to refetch through the normal, permission-checked views.

Targets are deliberately not part of an event. Who receives it is a server-side
routing decision (see :mod:`realtime.targets`) and must never travel inside a
public payload.
"""

import copy
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from django.utils import timezone


SCHEMA_VERSION = 1

# Values are part of the wire contract: they are stable, and business code must
# reference them through this enum instead of repeating string literals.
class RealtimeEventType(StrEnum):
    NOTIFICATION_CREATED = 'notification.created'
    NOTIFICATION_READ = 'notification.read'
    TASK_CREATED = 'task.created'
    TASK_UPDATED = 'task.updated'
    TASK_COMPLETED = 'task.completed'
    ACT_CREATED = 'act.created'
    ACT_UPDATED = 'act.updated'
    ACT_STATUS_CHANGED = 'act.status_changed'
    PROTOCOL_CREATED = 'protocol.created'
    PROTOCOL_UPDATED = 'protocol.updated'
    PROTOCOL_DELETED = 'protocol.deleted'
    PROTOCOL_STATUS_CHANGED = 'protocol.status_changed'
    PROTOCOL_APPROVAL_CHANGED = 'protocol.approval_changed'
    COMMENT_CREATED = 'comment.created'
    WORKUP_CREATED = 'workup.created'
    WORKUP_UPDATED = 'workup.updated'
    WORKUP_DELETED = 'workup.deleted'


# Resource types an event may describe. Kept small and explicit so a typo
# cannot silently invent a new resource namespace.
RESOURCE_ACT = 'act'
RESOURCE_COMMENT = 'comment'
RESOURCE_NOTIFICATION = 'notification'
RESOURCE_PROTOCOL = 'protocol'
RESOURCE_TASK = 'task'
RESOURCE_USER = 'user'
RESOURCE_WORKUP = 'workup'

RESOURCE_TYPES = frozenset(
    {
        RESOURCE_ACT,
        RESOURCE_COMMENT,
        RESOURCE_NOTIFICATION,
        RESOURCE_PROTOCOL,
        RESOURCE_TASK,
        RESOURCE_USER,
        RESOURCE_WORKUP,
    }
)

# Which «Проработка» row change a `workup.updated` describes. Technical
# metadata, not content: the client refetches the journal either way.
WORKUP_CHANGE_CONFIRMED = 'production_confirmed'
WORKUP_CHANGE_UNLOCKED = 'production_unlocked'

# Only these types may appear anywhere inside `data`. Everything else — model
# instances, datetimes, sets, bytes, Decimal — is rejected, which is what keeps
# payloads JSON-safe and free of accidental personal data.
JSON_SCALARS = (str, int, float, bool, type(None))

MAX_DATA_KEYS = 20
MAX_DATA_DEPTH = 3

# The exact set of keys on the wire. Deserialization accepts these and nothing
# else, so transport-only fields such as targets or a channel name cannot be
# injected into an event.
WIRE_FIELDS = frozenset(
    {
        'schema_version',
        'event_id',
        'event_type',
        'occurred_at',
        'resource_type',
        'resource_id',
        'data',
    }
)


class RealtimeEventError(ValueError):
    """An event that does not satisfy the contract."""


def _validate_json_safe(value, *, path, depth=0):
    if depth > MAX_DATA_DEPTH:
        raise RealtimeEventError(
            f'data{path}: превышена допустимая вложенность ({MAX_DATA_DEPTH}).'
        )
    # bool is a subclass of int, so it is checked by the scalar tuple already.
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise RealtimeEventError(f'data{path}: NaN и Infinity не являются JSON-значениями.')
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_safe(item, path=f'{path}[{index}]', depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise RealtimeEventError(f'data{path}: ключи должны быть непустыми строками.')
            _validate_json_safe(item, path=f'{path}.{key}', depth=depth + 1)
        return
    raise RealtimeEventError(
        f'data{path}: значение типа {type(value).__name__} не является JSON-безопасным.'
    )


@dataclass(frozen=True)
class RealtimeEvent:
    """One business fact, ready to be handed to any transport."""

    event_type: RealtimeEventType
    resource_type: str
    resource_id: int
    data: dict = field(default_factory=dict)
    event_id: uuid.UUID = field(default_factory=uuid.uuid4)
    occurred_at: datetime = field(default_factory=timezone.now)
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise RealtimeEventError('schema_version должен быть целым числом.')
        if self.schema_version < 1:
            raise RealtimeEventError('schema_version начинается с 1.')

        try:
            event_type = RealtimeEventType(self.event_type)
        except ValueError as exc:
            raise RealtimeEventError(f'Неизвестный тип события: {self.event_type!r}.') from exc
        object.__setattr__(self, 'event_type', event_type)

        if not isinstance(self.event_id, uuid.UUID):
            raise RealtimeEventError('event_id должен быть UUID.')

        if not isinstance(self.occurred_at, datetime):
            raise RealtimeEventError('occurred_at должен быть datetime.')
        if timezone.is_naive(self.occurred_at):
            raise RealtimeEventError('occurred_at должен содержать timezone.')

        if not isinstance(self.resource_type, str) or not self.resource_type.strip():
            raise RealtimeEventError('resource_type не может быть пустым.')
        if self.resource_type not in RESOURCE_TYPES:
            raise RealtimeEventError(
                f'Неизвестный resource_type: {self.resource_type!r}. '
                'Допустимы: ' + ', '.join(sorted(RESOURCE_TYPES)) + '.'
            )

        if isinstance(self.resource_id, bool) or not isinstance(self.resource_id, int):
            raise RealtimeEventError('resource_id должен быть целым числом.')
        if self.resource_id <= 0:
            raise RealtimeEventError('resource_id должен быть положительным.')

        if not isinstance(self.data, dict):
            raise RealtimeEventError('data должен быть словарём.')
        if len(self.data) > MAX_DATA_KEYS:
            raise RealtimeEventError(
                f'data содержит слишком много ключей (>{MAX_DATA_KEYS}); '
                'событие должно нести только минимальные метаданные.'
            )
        _validate_json_safe(self.data, path='')
        # The dataclass is frozen, but a dict passed in stays mutable for its
        # original owner. A defensive copy keeps the event genuinely immutable.
        object.__setattr__(self, 'data', copy.deepcopy(self.data))

    def as_dict(self):
        """Deterministic public payload. Never contains targets."""
        return {
            'schema_version': self.schema_version,
            'event_id': str(self.event_id),
            'event_type': self.event_type.value,
            'occurred_at': self.occurred_at.isoformat(),
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
            'data': copy.deepcopy(self.data),
        }

    def as_json(self):
        """Deterministic JSON: sorted keys, so equal events serialise equally."""
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False)

    def as_compact_json(self):
        """The same payload without padding — what actually goes on the wire."""
        return json.dumps(
            self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(',', ':')
        )

    def byte_size(self):
        """UTF-8 size of the wire payload, used by the transport size limit."""
        return len(self.as_compact_json().encode('utf-8'))

    @classmethod
    def from_dict(cls, payload, *, max_bytes=None):
        """Rebuild an event from a received payload, revalidating everything.

        A transport message is untrusted input: the wire format must match
        exactly, so unknown keys — including a smuggled `targets` or channel
        name — are rejected rather than ignored.
        """
        if not isinstance(payload, dict):
            raise RealtimeEventError('Событие должно быть объектом JSON.')

        keys = set(payload)
        missing = sorted(WIRE_FIELDS - keys)
        if missing:
            raise RealtimeEventError('В событии отсутствуют поля: ' + ', '.join(missing) + '.')
        unexpected = sorted(keys - WIRE_FIELDS)
        if unexpected:
            raise RealtimeEventError(
                'Событие содержит посторонние поля: ' + ', '.join(unexpected) + '.'
            )

        raw_event_id = payload['event_id']
        if not isinstance(raw_event_id, str):
            raise RealtimeEventError('event_id должен быть строкой UUID.')
        try:
            event_id = uuid.UUID(raw_event_id)
        except ValueError as exc:
            raise RealtimeEventError(f'Некорректный event_id: {raw_event_id!r}.') from exc

        raw_occurred_at = payload['occurred_at']
        if not isinstance(raw_occurred_at, str):
            raise RealtimeEventError('occurred_at должен быть строкой ISO-8601.')
        try:
            occurred_at = datetime.fromisoformat(raw_occurred_at)
        except ValueError as exc:
            raise RealtimeEventError(f'Некорректный occurred_at: {raw_occurred_at!r}.') from exc

        event = cls(
            event_type=payload['event_type'],
            resource_type=payload['resource_type'],
            resource_id=payload['resource_id'],
            data=payload['data'],
            event_id=event_id,
            occurred_at=occurred_at,
            schema_version=payload['schema_version'],
        )
        if event.schema_version != SCHEMA_VERSION:
            raise RealtimeEventError(
                f'Неподдерживаемая версия схемы события: {event.schema_version}. '
                f'Поддерживается только {SCHEMA_VERSION}.'
            )
        if max_bytes is not None and event.byte_size() > max_bytes:
            raise RealtimeEventError(
                f'Событие превышает допустимый размер: {event.byte_size()} > {max_bytes} байт.'
            )
        return event

    @classmethod
    def from_json(cls, raw, *, max_bytes=None):
        """Rebuild an event from raw JSON text or bytes received off the wire."""
        if isinstance(raw, (bytes, bytearray, memoryview)):
            encoded = bytes(raw)
            if max_bytes is not None and len(encoded) > max_bytes:
                raise RealtimeEventError(
                    f'Сообщение превышает допустимый размер: {len(encoded)} > {max_bytes} байт.'
                )
            try:
                text = encoded.decode('utf-8')
            except UnicodeDecodeError as exc:
                raise RealtimeEventError('Сообщение не является корректным UTF-8.') from exc
        elif isinstance(raw, str):
            text = raw
            if max_bytes is not None and len(text.encode('utf-8')) > max_bytes:
                raise RealtimeEventError(
                    'Сообщение превышает допустимый размер: '
                    f'{len(text.encode("utf-8"))} > {max_bytes} байт.'
                )
        else:
            raise RealtimeEventError(
                f'Ожидались строка или байты, получено {type(raw).__name__}.'
            )

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RealtimeEventError(f'Событие не является корректным JSON: {exc}.') from exc
        return cls.from_dict(payload, max_bytes=max_bytes)

    def log_context(self):
        """Non-sensitive fields safe to put in a log record."""
        return {
            'event_id': str(self.event_id),
            'event_type': self.event_type.value,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
        }
