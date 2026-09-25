import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import RunStatus, StepType
from app.models.mixins import CreatedAt, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.task import Task

DEFAULT_MAX_STEPS = 15


class Run(Base, UUIDPrimaryKey, CreatedAt):
    """One execution of an agent against a task."""

    __tablename__ = "runs"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[RunStatus] = mapped_column(
        SAEnum(RunStatus, name="run_status", values_callable=lambda e: [m.value for m in e]),
        default=RunStatus.QUEUED,
        server_default=RunStatus.QUEUED.value,
        index=True,
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    agent_model: Mapped[str] = mapped_column(String(128), nullable=False)
    max_steps: Mapped[int] = mapped_column(
        Integer, default=DEFAULT_MAX_STEPS, server_default=str(DEFAULT_MAX_STEPS), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)

    task: Mapped["Task"] = relationship(back_populates="runs")
    steps: Mapped[list["Step"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Step.step_number",
    )
    diffs: Mapped[list["Diff"]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    score: Mapped["Score | None"] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (CheckConstraint("max_steps > 0", name="ck_runs_max_steps_positive"),)

    def __repr__(self) -> str:
        return f"<Run {self.id} {self.status.value}>"


class Step(Base, UUIDPrimaryKey, CreatedAt):
    """A single thought / action / observation emitted by the agent loop."""

    __tablename__ = "steps"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[StepType] = mapped_column(
        SAEnum(StepType, name="step_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    content: Mapped[str | None] = mapped_column(Text)
    tool_name: Mapped[str | None] = mapped_column(String(128))
    tool_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    tool_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    run: Mapped["Run"] = relationship(back_populates="steps")

    # Steps stream in order and are read back in order; the composite unique
    # index enforces that ordering and serves the per-run listing query.
    __table_args__ = (
        UniqueConstraint("run_id", "step_number", name="uq_steps_run_step_number"),
    )

    def __repr__(self) -> str:
        return f"<Step {self.step_number} {self.type.value}>"


class Diff(Base, UUIDPrimaryKey, CreatedAt):
    """Unified diff for one file touched by a run."""

    __tablename__ = "diffs"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    diff_text: Mapped[str] = mapped_column(Text, nullable=False)

    run: Mapped["Run"] = relationship(back_populates="diffs")

    def __repr__(self) -> str:
        return f"<Diff {self.file_path}>"


class Score(Base, UUIDPrimaryKey, CreatedAt):
    """Evaluation result for a completed run. One row per run."""

    __tablename__ = "scores"

    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("runs.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    tests_passed: Mapped[int | None] = mapped_column(Integer)
    tests_total: Mapped[int | None] = mapped_column(Integer)
    step_count: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    llm_judge_score: Mapped[float | None] = mapped_column(Float)
    llm_judge_reasoning: Mapped[str | None] = mapped_column(Text)

    run: Mapped["Run"] = relationship(back_populates="score")

    __table_args__ = (
        CheckConstraint(
            "llm_judge_score IS NULL OR (llm_judge_score >= 1 AND llm_judge_score <= 10)",
            name="ck_scores_judge_range",
        ),
    )

    def __repr__(self) -> str:
        return f"<Score run={self.run_id} judge={self.llm_judge_score}>"
