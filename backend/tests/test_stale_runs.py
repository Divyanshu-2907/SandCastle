"""The stale-run sweeper: the only thing that closes out orphaned runs.

Nothing else can. A run stuck in `queued` means the broker lost the message, and
a run stuck in `running` means the worker died without running its `finally` --
in both cases there is no process left to notice.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Run, RunStatus, Task, User


@pytest.fixture
def sweep(db: Session, monkeypatch):
    """`sweep_stale_runs`, rewired to the test transaction.

    The task opens its own `SessionLocal`, which would reach the real database
    outside this test's rollback. Hand it the test session instead, and capture
    the Redis publishes rather than needing a live broker.
    """
    import app.jobs as jobs

    class Borrowed:
        def __enter__(self) -> Session:
            return db

        def __exit__(self, *_exc) -> bool:
            return False

    published: list[tuple[str, str]] = []
    monkeypatch.setattr(jobs, "SessionLocal", Borrowed)
    monkeypatch.setattr(
        jobs, "publish_status", lambda rid, status, **_kw: published.append((str(rid), status))
    )
    return jobs.sweep_stale_runs, published


def _make_run(db: Session, *, status: RunStatus, age_seconds: float, started: bool = True) -> Run:
    """A run whose timestamps are backdated by `age_seconds`."""
    user = User(email=f"{uuid.uuid4().hex[:8]}@example.com", hashed_password="x")
    db.add(user)
    db.flush()
    task = Task(user_id=user.id, repo_url="https://example.com/r.git", description="d")
    db.add(task)
    db.flush()

    then = datetime.now(UTC) - timedelta(seconds=age_seconds)
    run = Run(task_id=task.id, status=status, agent_model="test-model", max_steps=3)
    db.add(run)
    db.flush()
    # created_at has a server default, so it has to be overwritten after insert.
    run.created_at = then
    run.started_at = then if started else None
    db.flush()
    return run


def test_abandoned_running_run_is_failed(db: Session, sweep) -> None:
    sweep_stale_runs, published = sweep
    run = _make_run(
        db, status=RunStatus.RUNNING, age_seconds=settings.running_stale_after_seconds + 60
    )

    result = sweep_stale_runs()

    db.refresh(run)
    assert result == {"never_started": 0, "abandoned": 1}
    assert run.status is RunStatus.FAILED
    assert run.completed_at is not None
    assert "worker" in run.error_message
    # Clients still holding a WebSocket must be told, or they wait forever.
    assert published == [(str(run.id), "failed")]


def test_never_started_queued_run_is_failed(db: Session, sweep) -> None:
    sweep_stale_runs, published = sweep
    run = _make_run(
        db,
        status=RunStatus.QUEUED,
        age_seconds=settings.queued_stale_after_seconds + 60,
        started=False,
    )

    result = sweep_stale_runs()

    db.refresh(run)
    assert result == {"never_started": 1, "abandoned": 0}
    assert run.status is RunStatus.FAILED
    assert "lost" in run.error_message
    assert published == [(str(run.id), "failed")]


def test_running_with_null_started_at_still_swept(db: Session, sweep) -> None:
    """The worker died between setting `running` and writing `started_at`.

    A plain `started_at < cutoff` would compare against NULL, never match, and
    leave the row stuck forever -- hence the coalesce onto created_at.
    """
    sweep_stale_runs, _ = sweep
    run = _make_run(
        db,
        status=RunStatus.RUNNING,
        age_seconds=settings.running_stale_after_seconds + 60,
        started=False,
    )

    assert sweep_stale_runs()["abandoned"] == 1
    db.refresh(run)
    assert run.status is RunStatus.FAILED


def test_live_and_recent_runs_are_left_alone(db: Session, sweep) -> None:
    """Marking a healthy run dead is worse than reporting a dead one late."""
    sweep_stale_runs, published = sweep
    young_running = _make_run(db, status=RunStatus.RUNNING, age_seconds=5)
    young_queued = _make_run(db, status=RunStatus.QUEUED, age_seconds=5, started=False)
    # Just inside the deadline: the boundary is where an off-by-one would show.
    borderline = _make_run(
        db, status=RunStatus.RUNNING, age_seconds=settings.running_stale_after_seconds - 30
    )

    assert sweep_stale_runs() == {"never_started": 0, "abandoned": 0}

    for run in (young_running, young_queued, borderline):
        db.refresh(run)
    assert young_running.status is RunStatus.RUNNING
    assert young_queued.status is RunStatus.QUEUED
    assert borderline.status is RunStatus.RUNNING
    assert published == []


def test_terminal_runs_are_never_touched(db: Session, sweep) -> None:
    """An old completed run is old by definition; it is not stale."""
    sweep_stale_runs, published = sweep
    done = _make_run(db, status=RunStatus.COMPLETED, age_seconds=86_400)
    failed = _make_run(db, status=RunStatus.FAILED, age_seconds=86_400)

    assert sweep_stale_runs() == {"never_started": 0, "abandoned": 0}

    db.refresh(done)
    db.refresh(failed)
    assert done.status is RunStatus.COMPLETED
    assert failed.error_message is None
    assert published == []


def test_sweep_is_idempotent(db: Session, sweep) -> None:
    """A second pass must not re-fail or re-publish what the first one closed.

    The conditional UPDATE is what makes this true, and it is the same property
    that keeps two concurrent sweepers from both claiming a row.
    """
    sweep_stale_runs, published = sweep
    _make_run(db, status=RunStatus.RUNNING, age_seconds=settings.running_stale_after_seconds + 60)

    first = sweep_stale_runs()
    second = sweep_stale_runs()

    assert first["abandoned"] == 1
    assert second == {"never_started": 0, "abandoned": 0}
    assert len(published) == 1
