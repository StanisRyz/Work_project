"""Server-side routing targets.

A target answers *who may be handed this event*. It is always computed on the
server from the existing business rules — a client parameter must never
influence it — and it never travels inside the public event payload.

Two kinds exist for now:

* ``user:<id>`` — the authoritative per-recipient target;
* ``act:<id>`` — an additional routing hint for act-scoped events. A transport
  that ever exposes act rooms must still authorise the subscription with
  ``acts.permissions.can_view_act``; the target alone grants nothing.
"""

from dataclasses import dataclass


TARGET_USER = 'user'
TARGET_ACT = 'act'

TARGET_KINDS = frozenset({TARGET_USER, TARGET_ACT})


class RealtimeTargetError(ValueError):
    """A target that cannot be routed safely."""


@dataclass(frozen=True, order=True)
class RealtimeTarget:
    """One routing destination, e.g. ``user:7``."""

    kind: str
    identifier: int

    def __post_init__(self):
        if self.kind not in TARGET_KINDS:
            raise RealtimeTargetError(
                f'Неизвестный тип target: {self.kind!r}. '
                'Допустимы: ' + ', '.join(sorted(TARGET_KINDS)) + '.'
            )
        if isinstance(self.identifier, bool) or not isinstance(self.identifier, int):
            raise RealtimeTargetError(f'Идентификатор target должен быть целым числом: {self.identifier!r}.')
        if self.identifier <= 0:
            raise RealtimeTargetError(
                f'Идентификатор target должен быть положительным: {self.identifier!r}.'
            )

    @property
    def key(self):
        return f'{self.kind}:{self.identifier}'

    def __str__(self):
        return self.key


def _identifier_of(value):
    """Return a primary key from a model instance or a raw id."""
    return getattr(value, 'pk', value)


def user_target(user_or_id):
    """Return ``user:<id>``, or None when there is no user at all."""
    identifier = _identifier_of(user_or_id)
    if identifier is None:
        return None
    return RealtimeTarget(TARGET_USER, identifier)


def act_target(act_or_id):
    """Return ``act:<id>``, or None when there is no act at all."""
    identifier = _identifier_of(act_or_id)
    if identifier is None:
        return None
    return RealtimeTarget(TARGET_ACT, identifier)


def user_targets(users_or_ids):
    """Return user targets for an iterable, skipping None entries."""
    return [target for target in (user_target(item) for item in users_or_ids) if target is not None]


def normalize_targets(targets):
    """Return a deduplicated, deterministically ordered tuple of targets.

    None entries are ignored; an invalid identifier is an error, not something
    to skip quietly — a zero or negative id means the caller built the target
    from unsaved or corrupted data.
    """
    if targets is None:
        return ()
    if isinstance(targets, RealtimeTarget):
        targets = [targets]
    unique = set()
    for item in targets:
        if item is None:
            continue
        if not isinstance(item, RealtimeTarget):
            raise RealtimeTargetError(
                f'Ожидался RealtimeTarget, получено {type(item).__name__}.'
            )
        unique.add(item)
    # Ordered by (kind, identifier) thanks to the dataclass field order, so the
    # same audience always produces the same sequence.
    return tuple(sorted(unique))
