"""Controllable fake Redis clients.

The whole test suite runs without a Redis server: these fakes implement exactly
the surface the transport uses — `publish`, `ping`, `pubsub`, `subscribe`,
`unsubscribe`, `get_message`, `close`/`aclose` — and let a test script errors,
timeouts and malformed payloads deterministically.
"""

import asyncio

from redis import exceptions as redis_exceptions


class FakeSyncRedis:
    """Synchronous client used by the publisher and the diagnostic command."""

    def __init__(self, *, subscribers=1, publish_error=None, ping_error=None):
        self.published = []
        self.subscribers = subscribers
        self.publish_error = publish_error
        self.ping_error = ping_error
        self.closed = False
        self.pubsubs = []

    def publish(self, channel, message):
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((channel, message))
        for pubsub in self.pubsubs:
            pubsub.deliver(channel, message)
        return self.subscribers

    def ping(self):
        if self.ping_error is not None:
            raise self.ping_error
        return True

    def pubsub(self, ignore_subscribe_messages=False):
        pubsub = FakeSyncPubSub(ignore_subscribe_messages=ignore_subscribe_messages)
        self.pubsubs.append(pubsub)
        return pubsub

    def close(self):
        self.closed = True


class FakeSyncPubSub:
    def __init__(self, *, ignore_subscribe_messages=False):
        self.ignore_subscribe_messages = ignore_subscribe_messages
        self.subscribed = []
        self.unsubscribed = []
        self.closed = False
        self.messages = []
        self.get_message_error = None

    def subscribe(self, *channels):
        self.subscribed.extend(channels)

    def unsubscribe(self, *channels):
        self.unsubscribed.extend(channels)

    def deliver(self, channel, message):
        if channel in self.subscribed:
            payload = message if isinstance(message, bytes) else str(message).encode('utf-8')
            self.messages.append({'type': 'message', 'channel': channel, 'data': payload})

    def get_message(self, ignore_subscribe_messages=False, timeout=0.0):
        if self.get_message_error is not None:
            raise self.get_message_error
        if self.messages:
            return self.messages.pop(0)
        return None

    def close(self):
        self.closed = True


class FakeAsyncPubSub:
    """Async PubSub driven by a scripted list of messages.

    A scripted entry may be a dict (a message), None (a timeout, which the
    stream turns into a heartbeat) or an exception instance (raised).
    """

    def __init__(self, script=None, honour_timeout=False):
        self.script = list(script or [])
        # When set, an exhausted script waits out the caller's timeout exactly
        # like a real client would, so elapsed-time behaviour can be tested.
        self.honour_timeout = honour_timeout
        self.subscribed = []
        self.unsubscribed = []
        self.closed = False
        self.get_message_calls = 0
        self.unsubscribe_error = None
        self.close_error = None

    async def subscribe(self, *channels):
        self.subscribed.extend(channels)

    async def unsubscribe(self, *channels):
        if self.unsubscribe_error is not None:
            raise self.unsubscribe_error
        self.unsubscribed.extend(channels)

    async def get_message(self, ignore_subscribe_messages=False, timeout=None):
        self.get_message_calls += 1
        if not self.script:
            # Nothing left to say: behave like a quiet channel so the stream
            # keeps heartbeating instead of spinning.
            await asyncio.sleep(timeout if self.honour_timeout and timeout else 0)
            return None
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        await asyncio.sleep(0)
        return item

    async def aclose(self):
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


class FakeAsyncRedis:
    """Async client used by the SSE stream and its connectivity probe."""

    def __init__(self, *, script=None, ping_error=None, ping_delay=None, honour_timeout=False):
        self.pubsub_instance = FakeAsyncPubSub(script, honour_timeout=honour_timeout)
        self.ping_error = ping_error
        self.ping_delay = ping_delay
        self.closed = False
        self.close_error = None

    def pubsub(self, ignore_subscribe_messages=False):
        self.pubsub_instance.ignore_subscribe_messages = ignore_subscribe_messages
        return self.pubsub_instance

    async def ping(self):
        if self.ping_delay:
            await asyncio.sleep(self.ping_delay)
        if self.ping_error is not None:
            raise self.ping_error
        return True

    async def aclose(self):
        if self.close_error is not None:
            raise self.close_error
        self.closed = True


def message(channel, payload):
    """A Redis Pub/Sub message as the client delivers it."""
    data = payload if isinstance(payload, bytes) else str(payload).encode('utf-8')
    return {'type': 'message', 'channel': channel, 'data': data}


def subscribe_confirmation(channel):
    """The bookkeeping frame the stream must ignore."""
    return {'type': 'subscribe', 'channel': channel, 'data': 1}


def connection_error(text='Error 111 connecting to 127.0.0.1:6379.'):
    return redis_exceptions.ConnectionError(text)


def timeout_error(text='Timeout reading from socket'):
    return redis_exceptions.TimeoutError(text)
