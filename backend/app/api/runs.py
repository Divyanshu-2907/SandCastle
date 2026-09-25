"""Reading run status, steps, and diffs."""

import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import CurrentUser, DbSession
from app.models import Run, RunStatus, Task, User
from app.schemas import RunDetail, RunSummary

router = APIRouter(prefix="/runs", tags=["runs"])


def get_owned_run(db: DbSession, user: User, run_id: uuid.UUID, *, with_children: bool) -> Run:
    """Fetch a run the user owns, joined through its task. 404 if not theirs."""
    options = [selectinload(Run.task)]
    if with_children:
        options += [selectinload(Run.steps), selectinload(Run.diffs)]

    run = db.scalar(
        select(Run)
        .join(Task, Run.task_id == Task.id)
        .options(*options)
        .where(Run.id == run_id, Task.user_id == user.id)
    )
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run


@router.get("", response_model=list[RunSummary])
def list_runs(
    db: DbSession,
    user: CurrentUser,
    run_status: RunStatus | None = Query(default=None, alias="status"),
    limit: int = 50,
    offset: int = 0,
) -> list[Run]:
    """Run history for the current user, newest first."""
    query = (
        select(Run)
        .join(Task, Run.task_id == Task.id)
        .options(selectinload(Run.task))
        .where(Task.user_id == user.id)
    )
    if run_status is not None:
        query = query.where(Run.status == run_status)

    return list(
        db.scalars(
            query.order_by(Run.created_at.desc()).limit(min(limit, 200)).offset(offset)
        )
    )


@router.get("/{run_id}", response_model=RunDetail)
def get_run(run_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Run:
    """Status plus every step and diff recorded so far.

    Safe to poll while a run is in flight -- steps are committed as they happen.
    Phase 4 adds a WebSocket so polling becomes optional.
    """
    return get_owned_run(db, user, run_id, with_children=True)
