"""Redis channel names.

A channel name is always derived on the server from a validated
:class:`~realtime.targets.RealtimeTarget`. There is deliberately no function
that turns an arbitrary string into a channel: a client must never be able to
name the channel it subscribes to or publishes into.
"""

import re

from django.conf import settings

from .targets import RealtimeTarget, act_target, user_target


DEFAULT_PREFIX = 'quality-ecosystem:realtime'

# Printable ASCII, no whitespace and no control characters, so a prefix can
# never smuggle a newline or a Redis glob pattern into a channel name.
PREFIX_PATTERN = re.compile(r'^[A-Za-z0-9._:-]{1,64}$')

DIAGNOSTIC_SEGMENT = 'diagnostic'
DIAGNOSTIC_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9-]{8,64}$')


class RealtimeChannelError(ValueError):
    """A channel name that cannot be built safely."""


def normalize_channel_prefix(prefix=None):
    """Return a validated namespace prefix without a trailing separator."""
    raw = getattr(settings, 'REALTIME_CHANNEL_PREFIX', DEFAULT_PREFIX) if prefix is None else prefix
    text = str(raw or '').strip()
    if not text:
        raise RealtimeChannelError('REALTIME_CHANNEL_PREFIX не может быть пустым.')
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        raise RealtimeChannelError('REALTIME_CHANNEL_PREFIX содержит недопустимые символы.')
    text = text.rstrip(':')
    if not text or not PREFIX_PATTERN.match(text):
        raise RealtimeChannelError(
            f'Недопустимый REALTIME_CHANNEL_PREFIX: {raw!r}. '
            'Допустимы латинские буквы, цифры и символы . _ : - (до 64 символов).'
        )
    return text


def channel_for_target(target, prefix=None):
    """Return `<prefix>:<target.key>` for one validated target."""
    if not isinstance(target, RealtimeTarget):
        raise RealtimeChannelError(
            f'Канал строится только из RealtimeTarget, получено {type(target).__name__}.'
        )
    return f'{normalize_channel_prefix(prefix)}:{target.key}'


def channels_for_targets(targets, prefix=None):
    """Return one channel per target, deduplicated, in the targets' own order."""
    namespace = normalize_channel_prefix(prefix)
    channels = []
    for target in targets:
        channel = channel_for_target(target, namespace)
        if channel not in channels:
            channels.append(channel)
    return tuple(channels)


def user_channel(user_or_id, prefix=None):
    """The single channel an authenticated user may ever be subscribed to."""
    target = user_target(user_or_id)
    if target is None:
        raise RealtimeChannelError('Нельзя построить канал пользователя без идентификатора.')
    return channel_for_target(target, prefix)


def act_channel(act_or_id, prefix=None):
    """Channel for an act-scoped routing hint.

    RT-2 publishes to it but never lets a client subscribe: an act room needs
    `acts.permissions.can_view_act` authorisation, which is an RT-3 concern.
    """
    target = act_target(act_or_id)
    if target is None:
        raise RealtimeChannelError('Нельзя построить канал акта без идентификатора.')
    return channel_for_target(target, prefix)


def diagnostic_channel(token, prefix=None):
    """A throwaway channel for `check_realtime_transport`.

    Deliberately outside the `user:`/`act:` namespaces so a diagnostic run can
    never touch a real recipient's channel.
    """
    text = str(token or '')
    if not DIAGNOSTIC_TOKEN_PATTERN.match(text):
        raise RealtimeChannelError('Недопустимый диагностический token.')
    return f'{normalize_channel_prefix(prefix)}:{DIAGNOSTIC_SEGMENT}:{text}'
