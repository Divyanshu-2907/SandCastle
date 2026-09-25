"""Redis pub/sub fan-out for live run progress.

The worker publishes; WebSocket connections subscribe. Redis is the broker rather
than in-process state because the publisher (Celery worker) and the subscriber
(API process) are different containers.

Events are advisory. Postgres remains the source of truth -- a client that misses
a message can always re-read `GET /runs/{id}`.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Any

import redis
import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

# Published after a terminal status so subscribers know to stop waiting.
EVENT_STEP = "step"
EVENT_STATUS = "status"
EVENT_PING = "ping"

# How long a completed run's channel history stays meaningful. Pub/sub has no
# history, so this only bounds the terminal-status marker below.
TERMINAL_TTL_SECONDS = 60 * 60


def channel_for(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}"


def _terminal_key(run_id: uuid.UUID | str) -> str:
    return f"run:{run_id}:terminal"


def _encode(event: str, data: dict[str, Any]) -> str:
    return json.dumps({"event": event, "data": data}, default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


# -- publishing (worker side, synchronous) --------------------------------


_publisher: redis.Redis | None = None


def get_publisher() -> redis.Redis:
    global _publisher
    if _publisher is None:
        _publisher = redis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _publisher


def publish(run_id: uuid.UUID | str, event: str, data: dict[str, Any]) -> None:
    """Fire-and-forget. A dead Redis must never fail a run that is otherwise fine."""
    try:
        client = get_publisher()
        payload = _encode(event, data)
        client.publish(channel_for(run_id), payload)
        if event == EVENT_STATUS and data.get("status") in {"completed", "failed", "cancelled"}:
            # Lets a client that connects *after* the run ended learn it is over
            # without waiting for a message that will never come.
            client.setex(_terminal_key(run_id), TERMINAL_TTL_SECONDS, payload)
    except redis.RedisError:
        logger.warning("could not publish %s for run %s", event, run_id, exc_info=True)


def publish_step(run_id: uuid.UUID | str, step: dict[str, Any]) -> None:
    publish(run_id, EVENT_STEP, step)


def publish_status(run_id: uuid.UUID | str, status: str, **extra: Any) -> None:
    publish(run_id, EVENT_STATUS, {"status": status, **extra})


# -- subscribing (API side, async) ----------------------------------------


def get_subscriber_client() -> aioredis.Redis:
    """A fresh client per connection: pub/sub puts a connection into a mode
    where it can't serve anything else, so it must not be shared."""
    return aioredis.Redis.from_url(settings.redis_url, decode_responses=True)


async def read_terminal_marker(client: aioredis.Redis, run_id: uuid.UUID | str) -> str | None:
    try:
        return await client.get(_terminal_key(run_id))
    except redis.RedisError:
        return None
