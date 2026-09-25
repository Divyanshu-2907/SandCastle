import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.mixins import CreatedAt, UUIDPrimaryKey

if TYPE_CHECKING:
    from app.models.run import Run
    from app.models.user import User

EMBEDDING_DIM = 1536


class Task(Base, UUIDPrimaryKey, CreatedAt):
    __tablename__ = "tasks"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    repo_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    user: Mapped["User"] = relationship(back_populates="tasks")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Run.created_at",
    )
    embedding: Mapped["TaskEmbedding | None"] = relationship(
        back_populates="task", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Task {self.id} {self.description[:40]!r}>"


class TaskEmbedding(Base, UUIDPrimaryKey, CreatedAt):
    """One embedding per task description, for pgvector similarity search."""

    __tablename__ = "task_embeddings"

    task_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)

    task: Mapped["Task"] = relationship(back_populates="embedding")

    # IVFFlat needs training data to be worth building; at portfolio scale an
    # exact cosine scan over a few thousand rows is fast enough, so the index
    # is here purely to make the ORDER BY plan explicit.
    __table_args__ = (
        Index(
            "ix_task_embeddings_embedding_cosine",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
