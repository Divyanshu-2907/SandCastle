"""Live run streaming over WebSocket.

    ws://localhost:8000/ws/runs/{run_id}?token=<jwt>

Message envelope, one JSON object per frame:

    {"event": "step",   "data": {...}}   a newly recorded step
    {"event": "status", "data": {"status": "running"}}
    {"event": "ping",   "data": {}}      keepalive, safe to ignore

Ordering guarantee: the connection subscribes to Redis *before* it reads the
backlog out of Postgres, then drops any live step it already replayed. Doing it
the other way round loses every step emitted between the read and the subscribe.

The channel is server-to-client only. Nothing is read from the socket: a client
that goes away is noticed when the next send fails, which the keepalive bounds to
`KEEPALIVE_SECONDS`. Reading concurrently would detect it marginally sooner at
the cost of a second task racing the forward loop.
"""

import contextlib
import json
import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import TokenError, decode_access_token
from app.db.session import get_db
from app.events import (
    EVENT_PING,
    EVENT_STATUS,
    EVENT_STEP,
    channel_for,
    get_subscriber_client,
    read_terminal_marker,
)
from app.jobs import serialise_step
from app.models import Run, Task, User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["websocket"])

# Idle frames keep proxies from closing a quiet connection, and double as the
# heartbeat that notices a client which vanished without a close frame.
KEEPALIVE_SECONDS = 15.0

TERMINAL = {"completed", "failed", "cancelled"}

CLOSE_UNAUTHORIZED = status.WS_1008_POLICY_VIOLATION


def _authorize(
    db: Session, token: str | None, run_id: uuid.UUID
) -> tuple[str, list[dict[str, Any]]] | None:
    """Return (status, backlog) if this token may watch this run, else None.

    Two indexed lookups on connect -- short enough to run inline on the event
    loop rather than paying for a thread hop on every connection.
    """
    if not token:
        return None
    try:
        user_id = decode_access_token(token)
    except TokenError:
        return None

    run = db.scalar(
        select(Run)
        .join(Task, Run.task_id == Task.id)
        .options(selectinload(Run.steps))
        .where(Run.id == run_id, Task.user_id == user_id)
    )
    if run is None:
        # Unknown run and someone else's run are the same answer, as with the
        # REST endpoints: distinguishing them would confirm the id exists.
        return None
    if db.scalar(select(User.id).where(User.id == user_id)) is None:
        return None
    return run.status.value, [serialise_step(s) for s in run.steps]


@router.websocket("/ws/runs/{run_id}")
async def stream_run(
    websocket: WebSocket,
    run_id: uuid.UUID,
    db: Annotated[Session, Depends(get_db)],
    token: Annotated[str | None, Query(description="JWT access token")] = None,
) -> None:
    # Browsers cannot set headers on a WebSocket handshake, so the token comes in
    # the query string; a header is accepted too for CLI clients like wscat.
    if token is None:
        header = websocket.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header[7:]

    await websocket.accept()

    redis_client = get_subscriber_client()
    pubsub = redis_client.pubsub()

    try:
        # 1. Subscribe first, so nothing emitted during the backlog read is lost.
        await pubsub.subscribe(channel_for(run_id))

        # 2. Authorize and read the backlog.
        authorized = _authorize(db, token, run_id)
        if authorized is None:
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Not authorized for this run")
            return
        run_status, backlog = authorized

        # 3. Replay what already happened, then announce the current status.
        for step in backlog:
            await websocket.send_json({"event": EVENT_STEP, "data": step})
        replayed_through = max((s["step_number"] for s in backlog), default=0)
        await websocket.send_json({"event": EVENT_STATUS, "data": {"status": run_status}})

        if run_status in TERMINAL:
            # Nothing more will ever arrive on this channel.
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            return

        # A run that finished between the backlog read and the subscribe would
        # otherwise leave the client waiting for a message that never comes.
        marker = await read_terminal_marker(redis_client, run_id)
        if marker is not None:
            await websocket.send_text(marker)
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            return

        # 4. Live tail.
        await _forward(websocket, pubsub, replayed_through)

    except WebSocketDisconnect:
        logger.debug("client disconnected from run %s", run_id)
    except Exception:  # noqa: BLE001 - never leak a traceback onto the socket
        logger.exception("websocket error on run %s", run_id)
        with contextlib.suppress(Exception):
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
    finally:
        # Always give the subscription back, or Redis accumulates dead ones.
        with contextlib.suppress(Exception):
            await pubsub.unsubscribe(channel_for(run_id))
            await pubsub.aclose()
        with contextlib.suppress(Exception):
            await redis_client.aclose()


async def _forward(websocket: WebSocket, pubsub: Any, replayed_through: int) -> None:
    """Pump Redis messages to the socket until the run ends or the client leaves."""
    while True:
        message = await pubsub.get_message(timeout=KEEPALIVE_SECONDS)

        if message is None:
            # Genuinely idle. The send doubles as a liveness probe: if the client
            # is gone it raises, and the caller cleans up.
            await websocket.send_json({"event": EVENT_PING, "data": {}})
            continue

        # Subscribe/unsubscribe confirmations are bookkeeping, not events -- and
        # must not be mistaken for an idle period.
        if message.get("type") != "message":
            continue

        raw = message.get("data")
        if not isinstance(raw, str):
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # One bad publisher must not take down the stream.
            logger.warning("dropping malformed event: %.120s", raw)
            continue

        # Skip anything the backlog already covered.
        if parsed.get("event") == EVENT_STEP:
            if parsed.get("data", {}).get("step_number", 0) <= replayed_through:
                continue

        await websocket.send_text(raw)

        if parsed.get("event") == EVENT_STATUS and parsed.get("data", {}).get("status") in TERMINAL:
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            return
