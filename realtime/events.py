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
    ACT_UPDATED = 'act.updated'
    ACT_STATUS_CHANGED = 'act.status_changed'
    COMMENT_CREATED = 'comment.created'


# Resource types an event may describe. Kept small and explicit so a typo
# cannot silently invent a new resource namespace.
RESOURCE_ACT = 'act'
RESOURCE_COMMENT = 'comment'
RESOURCE_NOTIFICATION = 'notification'
RESOURCE_TASK = 'task'
RESOURCE_USER = 'user'

RESOURCE_TYPES = frozenset(
    {RESOURCE_ACT, RESOURCE_COMMENT, RESOURCE_NOTIFICATION, RESOURCE_TASK, RESOURCE_USER}
)

# Only these types may appear anywhere inside `data`. Everything else — model
# instances, datetimes, sets, bytes, Decimal — is rejected, which is what keeps
# payloads JSON-safe and free of accidental personal data.
JSON_SCALARS = (str, int, float, bool, type(None))

MAX_DATA_KEYS = 20
MAX_DATA_DEPTH = 3


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

    def log_context(self):
        """Non-sensitive fields safe to put in a log record."""
        return {
            'event_id': str(self.event_id),
            'event_type': self.event_type.value,
            'resource_type': self.resource_type,
            'resource_id': self.resource_id,
        }
