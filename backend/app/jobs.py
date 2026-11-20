"""Background jobs. Currently: execute one agent run.

Steps are committed one at a time as the agent emits them, rather than batched at
the end, so `GET /runs/{id}` shows progress while the run is still going -- and so
a crashed run keeps whatever it had already done. Each committed step is also
published to Redis so WebSocket clients see it without polling.
"""

import logging
import shutil
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import and_, func, select, update
from sqlalchemy.orm import Session, selectinload

from agent.loop import AgentLoop
from agent.sandbox import SandboxError, sandbox_for
from agent.schemas import AgentStep
from agent.tracing import build_tracer, guard
from agent.workspace import (
    WorkspaceError,
    clone_repo,
    collect_file_diffs_via,
    prepare_git_identity,
)
from app.core.config import settings
from app.db.session import SessionLocal
from app.events import publish_status, publish_step
from app.models import Diff, Run, RunStatus, Step, StepType
from app.worker import celery_app

logger = logging.getLogger(__name__)


def serialise_step(step: Step) -> dict:
    """The wire shape for a step event -- matches the `StepRead` API schema."""
    return {
        "id": str(step.id),
        "run_id": str(step.run_id),
        "step_number": step.step_number,
        "type": step.type.value,
        "content": step.content,
        "tool_name": step.tool_name,
        "tool_input": step.tool_input,
        "tool_output": step.tool_output,
        "created_at": step.created_at.isoformat() if step.created_at else None,
    }


class StepRecorder:
    """Persists each agent step as it happens, then broadcasts it.

    Uses its own short-lived session per step so an open transaction never
    straddles a slow model call -- otherwise readers would block for minutes.

    Order matters: commit first, publish second. A subscriber that reacts to an
    event by re-reading the database must find the row already there.
    """

    def __init__(self, run_id: uuid.UUID) -> None:
        self.run_id = run_id
        self.count = 0

    def __call__(self, step: AgentStep) -> None:
        with SessionLocal() as db:
            row = Step(
                run_id=self.run_id,
                step_number=step.step_number,
                type=StepType(step.type),
                content=step.content,
                tool_name=step.tool_name,
                tool_input=step.tool_input,
                tool_output=step.tool_output,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            payload = serialise_step(row)

        self.count += 1
        publish_step(self.run_id, payload)


def _load_run(db: Session, run_id: uuid.UUID) -> Run | None:
    return db.scalar(select(Run).options(selectinload(Run.task)).where(Run.id == run_id))


def _mark(db: Session, run: Run, status: RunStatus, **fields) -> None:
    run.status = status
    for key, value in fields.items():
        setattr(run, key, value)
    db.commit()
    publish_status(
        run.id,
        status.value,
        error_message=fields.get("error_message"),
        started_at=fields.get("started_at"),
        completed_at=fields.get("completed_at"),
        stop_reason=fields.get("stop_reason"),
    )


@celery_app.task(name="app.jobs.run_agent_task", bind=True)
def run_agent_task(self, run_id: str) -> dict:  # noqa: ANN001 - Celery passes self
    """Clone the task's repo, drive the agent, record steps and diffs."""
    run_uuid = uuid.UUID(run_id)

    with SessionLocal() as db:
        run = _load_run(db, run_uuid)
        if run is None:
            logger.error("run %s no longer exists; nothing to do", run_id)
            return {"run_id": run_id, "status": "missing"}
        if run.status != RunStatus.QUEUED:
            # Guards against a duplicate delivery re-running finished work.
            logger.warning("run %s is %s, not queued; skipping", run_id, run.status)
            return {"run_id": run_id, "status": run.status.value}

        task = run.task
        repo_url, description = task.repo_url, task.description
        model, max_steps = run.agent_model, run.max_steps
        _mark(db, run, RunStatus.RUNNING, started_at=datetime.now(UTC))

    recorder = StepRecorder(run_uuid)
    # A NoopTracer unless both Langfuse keys are configured, so an unconfigured
    # deployment never imports the SDK, let alone reaches the network.
    tracer = build_tracer(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_endpoint,
    )
    root = Path(tempfile.mkdtemp(prefix=f"sandcastle-{run_uuid}-"))
    error: str | None = None
    stop_reason = "error"

    try:
        # The worker clones -- it is trusted and has egress. The sandbox gets the
        # bytes handed to it over the Docker API and never needs a network of
        # its own, which is why it can run with `network_mode=none`.
        checkout = clone_repo(repo_url, root / "repo")

        with sandbox_for(run_uuid, source=checkout) as box:
            backend = box.backend()
            prepare_git_identity(backend)

            result = AgentLoop(
                workspace=None,
                description=description,
                repo_url=repo_url,
                model=model,
                max_steps=max_steps,
                backend=backend,
                on_step=recorder,
                run_id=run_id,
                tracer=tracer,
            ).run()

            error = result.error
            stop_reason = result.stop_reason

            if box.timed_out:
                # The watchdog killed the container out from under the agent;
                # whatever it reported first is less true than this.
                error = (
                    f"Run exceeded its {box.limits.wall_clock_seconds}s wall-clock "
                    "limit and the sandbox was killed."
                )
                stop_reason = "sandbox_timeout"
                _record_failure_step(recorder, error)
            else:
                # Read the diff out before the container goes away.
                _save_diffs(run_uuid, backend)
    except SandboxError as exc:
        error = f"Sandbox failure: {exc}"
        _record_failure_step(recorder, error)
    except WorkspaceError as exc:
        error = str(exc)
        _record_failure_step(recorder, error)
    except SoftTimeLimitExceeded:
        # Celery's outer limit fired: something outside the sandbox hung (a
        # clone against a dead remote, an LLM socket that never closed). Caught
        # before the catch-all purely so the run says that, rather than
        # reporting a bare exception name.
        error = (
            f"Run exceeded its {settings.task_soft_time_limit}s task time limit "
            "and was stopped."
        )
        stop_reason = "task_timeout"
        _record_failure_step(recorder, error)
    except Exception as exc:  # noqa: BLE001 - a bad run must not kill the worker
        logger.exception("run %s crashed", run_id)
        error = f"{type(exc).__name__}: {exc}"
        _record_failure_step(recorder, error)
    finally:
        # Langfuse batches and exports on a background thread. A Celery task
        # that returns without flushing can have the worker move on -- or the
        # process be recycled -- before the batch leaves, losing the trace of
        # the run that just finished.
        guard("flush", tracer.flush)
        _cleanup(root)

    with SessionLocal() as db:
        run = _load_run(db, run_uuid)
        if run is not None:
            _mark(
                db,
                run,
                RunStatus.FAILED if error else RunStatus.COMPLETED,
                completed_at=datetime.now(UTC),
                error_message=error,
                # Recorded alongside the status, not folded into it: running out
                # of turns is not an error, but it is not success either.
                stop_reason=stop_reason,
            )

    return {
        "run_id": run_id,
        "status": "failed" if error else "completed",
        "steps": recorder.count,
        "stop_reason": stop_reason,
    }


def _save_diffs(run_id: uuid.UUID, backend) -> None:
    try:
        diffs = collect_file_diffs_via(backend)
    except Exception:  # noqa: BLE001 - a missing diff must not fail a good run
        logger.warning("could not collect diffs for run %s", run_id, exc_info=True)
        return
    if not diffs:
        return
    with SessionLocal() as db:
        db.add_all(
            Diff(run_id=run_id, file_path=d.file_path, diff_text=d.diff_text) for d in diffs
        )
        db.commit()


def _record_failure_step(recorder: StepRecorder, message: str) -> None:
    """Make the failure visible in the step stream, not only in run.error_message."""
    try:
        recorder(
            AgentStep(
                step_number=recorder.count + 1,
                type="observation",
                content=message,
                tool_output={"error": message},
            )
        )
    except Exception:  # noqa: BLE001 - the run already failed; don't fail the failure path
        logger.exception("could not record the failure step for run %s", recorder.run_id)


def _cleanup(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)


def _fail_stale(
    db: Session, status: RunStatus, cutoff_column, cutoff: datetime, message: str, now: datetime
) -> list[uuid.UUID]:
    """Fail every run of `status` older than `cutoff`, returning the ids changed.

    One conditional UPDATE rather than select-then-update: the `status` in the
    WHERE clause is the lock. Two sweepers racing (or a sweeper racing a worker
    that is finishing normally) cannot both claim the same row, so the loser
    updates nothing and nobody double-publishes a terminal status.
    """
    rows = db.execute(
        update(Run)
        .where(and_(Run.status == status, cutoff_column < cutoff))
        .values(status=RunStatus.FAILED, completed_at=now, error_message=message)
        .returning(Run.id),
        # No ORM objects are loaded here and the session is discarded straight
        # after, so there is no identity map worth synchronising -- and asking
        # for it would conflict with the RETURNING above.
        execution_options={"synchronize_session": False},
    )
    ids = [row[0] for row in rows]
    db.commit()
    return ids


@celery_app.task(name="app.jobs.sweep_stale_runs")
def sweep_stale_runs() -> dict:
    """Close out runs that no worker is going to finish.

    Two ways a run gets orphaned, neither of which the run itself can report:

    * `queued` forever -- the broker lost the message. Redis is not a durable
      queue, so a restart or an eviction drops whatever was in flight.
    * `running` forever -- the worker was SIGKILLed (OOM, VM preemption, a
      `docker kill`). `finally` does not run, so nothing marks the row.

    Sandbox containers from such a crash are reaped on worker startup; this is
    the database half of that same cleanup. Deliberately not a retry: there is
    no checkpoint to resume an agent run from, so the honest outcome is a
    failed run the user can re-queue, not a silent second attempt.

    A worker that was merely slow, not dead, will overwrite the row when it
    finishes. That is the right outcome -- the run really did complete -- and
    the only visible artefact is one spurious `failed` event in between.
    """
    now = datetime.now(UTC)
    with SessionLocal() as db:
        never_started = _fail_stale(
            db,
            RunStatus.QUEUED,
            Run.created_at,
            now - timedelta(seconds=settings.queued_stale_after_seconds),
            f"Never picked up by a worker within {settings.queued_stale_after_seconds}s; "
            "the queued task was most likely lost. Re-queue the run to try again.",
            now,
        )
        abandoned = _fail_stale(
            db,
            RunStatus.RUNNING,
            # A row can be `running` with a NULL started_at only if the process
            # died between the two writes; fall back to created_at so it is
            # still eligible instead of being skipped forever by a NULL compare.
            func.coalesce(Run.started_at, Run.created_at),
            now - timedelta(seconds=settings.running_stale_after_seconds),
            f"No progress for {settings.running_stale_after_seconds}s; the worker "
            "running it was lost. Re-queue the run to try again.",
            now,
        )

    # Published after the commit, same ordering rule as StepRecorder: a client
    # that reacts by re-reading the run must find the terminal row already
    # there. This also releases WebSocket clients still waiting on a run whose
    # worker vanished -- without it they wait for the keepalive to outlive them.
    for run_id in [*never_started, *abandoned]:
        publish_status(run_id, RunStatus.FAILED.value, completed_at=now)

    if never_started or abandoned:
        logger.warning(
            "swept %d never-started and %d abandoned run(s)",
            len(never_started),
            len(abandoned),
        )
    return {"never_started": len(never_started), "abandoned": len(abandoned)}


__all__ = ["StepRecorder", "run_agent_task", "serialise_step", "sweep_stale_runs"]
