"""Grading-task event distribution over pub/sub.

The batch-grading worker publishes state transitions; server-sent-event
subscribers wake on publish instead of polling the database while idle.
:class:`TaskEventPublisher` is the worker-facing view (publish only);
:class:`TaskEventBroker` adds the subscriber side for stream endpoints.
:class:`RedisTaskEventBroker` is the production adapter (the only module
besides the rate limiter allowed to import the ``redis`` client);
:class:`InMemoryTaskEventBroker` serves tests and degraded single-process
deployments.
"""

import asyncio
import logging
from typing import Protocol, runtime_checkable

from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def grading_task_channel(assignment_id: str) -> str:
    """Build the pub/sub channel key for an assignment's grading task.

    Args:
        assignment_id: Assignment id.

    Returns:
        Channel name.
    """
    return f"grading-task:{assignment_id}"


@runtime_checkable
class TaskEventPublisher(Protocol):
    """Publish-side port for grading-task state changes."""

    async def publish(self, channel: str, payload: str) -> None:
        """Publish one event to a channel.

        Args:
            channel: Channel key (see :func:`grading_task_channel`).
            payload: Opaque string payload (subscribers re-read state).
        """
        ...


class TaskEventBroker(TaskEventPublisher, Protocol):
    """Full pub/sub port for stream endpoints.

    One broker instance serves ONE subscription at a time (a stream holds a
    single channel); create a fresh instance per connection.
    """

    async def subscribe(self, channel: str) -> None:
        """Start listening on a channel.

        Args:
            channel: Channel key.
        """
        ...

    async def unsubscribe(self, channel: str) -> None:
        """Stop listening and release the subscription.

        Args:
            channel: Channel key.
        """
        ...

    async def wait_for_event(self, channel: str, wait_seconds: float) -> str | None:
        """Wait for one published event on the subscribed channel.

        Args:
            channel: Channel key (must match the ``subscribe`` call).
            wait_seconds: Maximum seconds to wait.

        Returns:
            The published payload, or ``None`` on timeout/backend trouble
            (callers treat ``None`` as a safety tick).
        """
        ...


class RedisTaskEventBroker:
    """:class:`TaskEventBroker` backed by Redis pub/sub.

    Uses a dedicated Redis client without aggressive socket timeouts so
    ``get_message`` can block for its full tick window; transient backend
    errors degrade to a ``None`` tick instead of killing the stream.
    """

    def __init__(self, client: AsyncRedis) -> None:
        """Bind the broker to a Redis client (publish + per-instance pubsub).

        Args:
            client: Async Redis client with ``decode_responses=True``.
        """
        self._client = client
        self._pubsub: PubSub | None = None
        self._channel: str | None = None

    async def publish(self, channel: str, payload: str) -> None:
        """Publish one event; failures surface to the caller.

        Args:
            channel: Channel key.
            payload: String payload.
        """
        await self._client.publish(channel, payload)

    async def subscribe(self, channel: str) -> None:
        """Open a pubsub connection and subscribe to the channel.

        Args:
            channel: Channel key.
        """
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(channel)
        self._channel = channel

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe and close the pubsub connection.

        Args:
            channel: Channel key.
        """
        if self._pubsub is None:
            return
        try:
            await self._pubsub.unsubscribe(channel)
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]
        except RedisError:  # pragma: no cover - environment-dependent
            logger.warning("Failed to close grading-task pubsub.", exc_info=True)
        self._pubsub = None
        self._channel = None

    async def wait_for_event(self, channel: str, wait_seconds: float) -> str | None:
        """Block for one message; ``None`` on timeout or backend trouble.

        Args:
            channel: Channel key (must match the ``subscribe`` call).
            wait_seconds: Maximum seconds to wait.

        Returns:
            The payload string, or ``None``.
        """
        if self._pubsub is None:
            await asyncio.sleep(wait_seconds)
            return None
        try:
            message = await self._pubsub.get_message(
                ignore_subscribe_messages=True, timeout=wait_seconds
            )
        except (RedisError, OSError):
            logger.warning("Grading-task pubsub read failed; degrading to tick.", exc_info=True)
            await asyncio.sleep(min(wait_seconds, 1.0))
            return None
        if message:
            payload = message.get("data")
            if isinstance(payload, str):
                return payload
        return None


class InMemoryTaskEventBroker:
    """:class:`TaskEventBroker` over asyncio queues.

    Process-local by construction: subscribers only receive events published
    to the same broker instance (tests wire a shared instance). Publishes to
    channels without subscribers are dropped, matching pub/sub semantics.
    """

    def __init__(self) -> None:
        """Initialize the empty subscription registry."""
        self._subscribers: dict[str, list[asyncio.Queue[str | None]]] = {}
        self._queue: asyncio.Queue[str | None] | None = None
        self._channel: str | None = None

    async def publish(self, channel: str, payload: str) -> None:
        """Deliver the payload to every live subscriber of the channel.

        Args:
            channel: Channel key.
            payload: String payload.
        """
        for queue in list(self._subscribers.get(channel, [])):
            queue.put_nowait(payload)

    async def subscribe(self, channel: str) -> None:
        """Register this instance's single subscription queue.

        Args:
            channel: Channel key.
        """
        self._queue = asyncio.Queue()
        self._channel = channel
        self._subscribers.setdefault(channel, []).append(self._queue)

    async def unsubscribe(self, channel: str) -> None:
        """Remove the subscription queue.

        Args:
            channel: Channel key.
        """
        queues = self._subscribers.get(channel, [])
        if self._queue is not None:
            queues[:] = [q for q in queues if q is not self._queue]
        if not queues:
            self._subscribers.pop(channel, None)
        self._queue = None
        self._channel = None

    async def wait_for_event(self, channel: str, wait_seconds: float) -> str | None:
        """Await one queued payload; ``None`` after the timeout.

        Args:
            channel: Channel key (must match the ``subscribe`` call).
            wait_seconds: Maximum seconds to wait.

        Returns:
            The payload string, or ``None``.
        """
        queue = self._queue if self._channel == channel else None
        if queue is None:
            await asyncio.sleep(wait_seconds)
            return None
        try:
            return await asyncio.wait_for(queue.get(), timeout=wait_seconds)
        except TimeoutError:
            return None
