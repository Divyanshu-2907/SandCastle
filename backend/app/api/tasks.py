"""Task creation and run submission."""

import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.models import Run, RunStatus, Task, User
from app.schemas import RunCreate, RunCreated, TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


def get_owned_task(db: DbSession, user: User, task_id: uuid.UUID) -> Task:
    """Fetch a task, 404ing if it doesn't exist *or* isn't this user's.

    Same status either way: a 403 would confirm the id exists.
    """
    task = db.scalar(select(Task).where(Task.id == task_id, Task.user_id == user.id))
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(payload: TaskCreate, db: DbSession, user: CurrentUser) -> Task:
    task = Task(
        user_id=user.id, repo_url=payload.repo_url, description=payload.description
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@router.get("", response_model=list[TaskRead])
def list_tasks(db: DbSession, user: CurrentUser, limit: int = 50, offset: int = 0) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .where(Task.user_id == user.id)
            .order_by(Task.created_at.desc())
            .limit(min(limit, 200))
            .offset(offset)
        )
    )


@router.get("/{task_id}", response_model=TaskRead)
def get_task(task_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Task:
    return get_owned_task(db, user, task_id)


@router.post("/{task_id}/runs", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED)
def create_run(
    task_id: uuid.UUID, db: DbSession, user: CurrentUser, payload: RunCreate | None = None
) -> Run:
    """Queue an agent run for this task and hand back its id immediately."""
    task = get_owned_task(db, user, task_id)
    payload = payload or RunCreate()

    run = Run(
        task_id=task.id,
        status=RunStatus.QUEUED,
        agent_model=payload.agent_model or settings.agent_model,
        max_steps=payload.max_steps or settings.agent_max_steps,
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    # Imported here so the API process never needs the worker's dependencies at
    # import time, and so tests can patch it.
    from app.jobs import run_agent_task

    run_agent_task.delay(str(run.id))
    return run
