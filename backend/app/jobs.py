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
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from agent.loop import AgentLoop
from agent.sandbox import SandboxError, sandbox_for
from agent.schemas import AgentStep
from agent.workspace import (
    WorkspaceError,
    clone_repo,
    collect_file_diffs_via,
    prepare_git_identity,
)
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
    except Exception as exc:  # noqa: BLE001 - a bad run must not kill the worker
        logger.exception("run %s crashed", run_id)
        error = f"{type(exc).__name__}: {exc}"
        _record_failure_step(recorder, error)
    finally:
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


__all__ = ["StepRecorder", "run_agent_task", "serialise_step"]
