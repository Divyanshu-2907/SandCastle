"""WebSocket streaming: auth, backlog replay, live tail, de-duplication."""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.events import EVENT_PING, EVENT_STATUS, EVENT_STEP, _encode, channel_for

GOOD_TASK = {
    "repo_url": "https://github.com/octocat/Hello-World.git",
    "description": "Add a build-status badge to the README.",
}


@pytest.fixture
def queued_run(auth_client: TestClient) -> str:
    task_id = auth_client.post("/tasks", json=GOOD_TASK).json()["id"]
    return auth_client.post(f"/tasks/{task_id}/runs").json()["id"]


@pytest.fixture
def token(auth_client: TestClient) -> str:
    return auth_client.headers["Authorization"].removeprefix("Bearer ")


def add_steps(db, run_id: str, count: int, start: int = 1) -> None:
    from app.models import Step, StepType

    db.add_all(
        Step(
            run_id=uuid.UUID(run_id),
            step_number=n,
            type=StepType.ACTION,
            tool_name="list_files",
            tool_input={"directory": "."},
        )
        for n in range(start, start + count)
    )
    db.flush()


def next_event(ws, limit: int = 10) -> dict:
    """Next non-keepalive frame. Pings are documented as ignorable."""
    for _ in range(limit):
        message = ws.receive_json()
        if message["event"] != EVENT_PING:
            return message
    raise AssertionError("only keepalives received")


def drain(ws, limit: int = 40) -> list[dict]:
    """Read frames until the server closes, ignoring keepalives."""
    from starlette.websockets import WebSocketDisconnect

    frames = []
    for _ in range(limit):
        try:
            message = ws.receive_json()
        except WebSocketDisconnect:
            break
        if message["event"] != EVENT_PING:
            frames.append(message)
    return frames


# -- authorization --------------------------------------------------------


def test_connection_without_a_token_is_rejected(client: TestClient, queued_run: str) -> None:
    from starlette.websockets import WebSocketDisconnect

    # `queued_run` authenticated the shared client; drop the header so this is a
    # genuinely anonymous handshake.
    client.headers.pop("Authorization", None)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/runs/{queued_run}") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_connection_with_a_garbage_token_is_rejected(
    client: TestClient, queued_run: str
) -> None:
    from starlette.websockets import WebSocketDisconnect

    client.headers.pop("Authorization", None)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/runs/{queued_run}?token=nonsense") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_another_users_run_is_rejected(
    client: TestClient, other_client: TestClient, queued_run: str
) -> None:
    from starlette.websockets import WebSocketDisconnect

    stranger_token = other_client.headers["Authorization"].removeprefix("Bearer ")
    client.headers.pop("Authorization", None)  # the query param must be what decides
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/runs/{queued_run}?token={stranger_token}") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_unknown_run_is_rejected(client: TestClient, token: str) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/ws/runs/{uuid.uuid4()}?token={token}") as ws:
            ws.receive_json()
    assert exc.value.code == 1008


def test_bearer_header_is_accepted_too(client: TestClient, token: str, queued_run: str) -> None:
    """CLI clients like wscat can send a header instead of a query param."""
    with client.websocket_connect(
        f"/ws/runs/{queued_run}", headers={"Authorization": f"Bearer {token}"}
    ) as ws:
        assert ws.receive_json()["event"] == EVENT_STATUS


# -- backlog --------------------------------------------------------------


def test_existing_steps_are_replayed_before_live_updates(
    client: TestClient, token: str, queued_run: str, db
) -> None:
    add_steps(db, queued_run, count=3)

    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        frames = [ws.receive_json() for _ in range(4)]

    assert [f["event"] for f in frames] == [EVENT_STEP] * 3 + [EVENT_STATUS]
    assert [f["data"]["step_number"] for f in frames[:3]] == [1, 2, 3]
    assert frames[3]["data"]["status"] == "queued"


def test_empty_backlog_still_reports_status(
    client: TestClient, token: str, queued_run: str
) -> None:
    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        frame = ws.receive_json()
    assert frame == {"event": EVENT_STATUS, "data": {"status": "queued"}}


def test_finished_run_sends_backlog_then_closes(
    client: TestClient, token: str, queued_run: str, db
) -> None:
    from app.models import Run, RunStatus

    add_steps(db, queued_run, count=2)
    db.query(Run).filter(Run.id == uuid.UUID(queued_run)).update({"status": RunStatus.COMPLETED})
    db.flush()

    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        frames = drain(ws)

    assert [f["event"] for f in frames] == [EVENT_STEP, EVENT_STEP, EVENT_STATUS]
    assert frames[-1]["data"]["status"] == "completed"


# -- live tail ------------------------------------------------------------


def test_live_steps_arrive_after_the_backlog(
    client: TestClient, token: str, queued_run: str, db
) -> None:
    from app.events import get_publisher

    add_steps(db, queued_run, count=1)

    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        assert next_event(ws)["data"]["step_number"] == 1  # backlog
        assert next_event(ws)["event"] == EVENT_STATUS

        publisher = get_publisher()
        publisher.publish(
            channel_for(queued_run),
            _encode(EVENT_STEP, {"step_number": 2, "type": "observation", "content": "live"}),
        )
        live = next_event(ws)

    assert live["event"] == EVENT_STEP
    assert live["data"]["step_number"] == 2
    assert live["data"]["content"] == "live"


def test_steps_already_replayed_are_not_sent_twice(
    client: TestClient, token: str, queued_run: str, db
) -> None:
    """The worker may publish a step the backlog read already picked up."""
    from app.events import get_publisher

    add_steps(db, queued_run, count=2)
    publisher = get_publisher()

    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        for expected in (1, 2):
            assert next_event(ws)["data"]["step_number"] == expected
        assert next_event(ws)["event"] == EVENT_STATUS

        # Re-publish step 2 (duplicate), then a genuinely new step 3.
        publisher.publish(
            channel_for(queued_run), _encode(EVENT_STEP, {"step_number": 2, "content": "dupe"})
        )
        publisher.publish(
            channel_for(queued_run), _encode(EVENT_STEP, {"step_number": 3, "content": "new"})
        )
        received = next_event(ws)

    assert received["data"]["step_number"] == 3
    assert received["data"]["content"] == "new"


def test_terminal_status_closes_the_connection(
    client: TestClient, token: str, queued_run: str
) -> None:
    from starlette.websockets import WebSocketDisconnect

    from app.events import get_publisher

    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        assert next_event(ws)["event"] == EVENT_STATUS

        get_publisher().publish(
            channel_for(queued_run), _encode(EVENT_STATUS, {"status": "completed"})
        )
        assert next_event(ws)["data"]["status"] == "completed"

        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_malformed_event_is_skipped_not_fatal(
    client: TestClient, token: str, queued_run: str
) -> None:
    from app.events import get_publisher

    publisher = get_publisher()
    with client.websocket_connect(f"/ws/runs/{queued_run}?token={token}") as ws:
        assert next_event(ws)["event"] == EVENT_STATUS

        publisher.publish(channel_for(queued_run), "{not json at all")
        publisher.publish(
            channel_for(queued_run), _encode(EVENT_STEP, {"step_number": 1, "content": "fine"})
        )
        assert next_event(ws)["data"]["content"] == "fine"


# -- event encoding -------------------------------------------------------


def test_encode_handles_uuid_and_datetime() -> None:
    from datetime import UTC, datetime

    payload = _encode(
        EVENT_STEP, {"id": uuid.uuid4(), "created_at": datetime(2026, 1, 2, 3, 4, tzinfo=UTC)}
    )
    decoded = json.loads(payload)
    assert decoded["data"]["created_at"].startswith("2026-01-02T03:04")
    assert uuid.UUID(decoded["data"]["id"])


def test_publish_survives_a_dead_redis(monkeypatch) -> None:
    """A broken broker must degrade the stream, not fail the run."""
    import redis

    from app import events

    class Broken:
        def publish(self, *_args):
            raise redis.ConnectionError("redis is down")

    monkeypatch.setattr(events, "get_publisher", lambda: Broken())
    events.publish_step(uuid.uuid4(), {"step_number": 1})  # must not raise
