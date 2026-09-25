"""Request and response models for the HTTP API.

Kept separate from the ORM models so the wire format can stay stable while the
schema underneath moves, and so password hashes can never leak into a response.
"""

import re
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.enums import RunStatus, StepType

MAX_DESCRIPTION_CHARS = 4_000

# git@host:path, https://host/path, or ssh://... -- enough to reject obvious junk
# without trying to be a URL parser. Phase 9 tightens this.
_REPO_URL_RE = re.compile(
    r"^(https?://[\w.-]+(:\d+)?/\S+|git@[\w.-]+:\S+|ssh://[\w.@-]+(:\d+)?/\S+)$"
)

Password = Annotated[str, Field(min_length=8, max_length=128)]


# -- auth -----------------------------------------------------------------


class UserCreate(BaseModel):
    email: EmailStr
    password: Password


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    created_at: datetime


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRead


# -- tasks ----------------------------------------------------------------


class TaskCreate(BaseModel):
    repo_url: str = Field(max_length=2048)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)

    @field_validator("repo_url")
    @classmethod
    def validate_repo_url(cls, value: str) -> str:
        value = value.strip()
        if not _REPO_URL_RE.match(value):
            raise ValueError(
                "repo_url must be an http(s), ssh, or git@host:path git URL"
            )
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description cannot be blank")
        return value


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    repo_url: str
    description: str
    created_at: datetime


# -- runs -----------------------------------------------------------------


class RunCreate(BaseModel):
    """Optional per-run overrides; both fall back to server config."""

    agent_model: str | None = Field(default=None, max_length=128)
    max_steps: int | None = Field(default=None, ge=1, le=50)


class StepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_number: int
    type: StepType
    content: str | None
    tool_name: str | None
    tool_input: dict[str, Any] | None
    tool_output: dict[str, Any] | None
    created_at: datetime


class DiffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_path: str
    diff_text: str
    created_at: datetime


class RunSummary(BaseModel):
    """Row shape for the run-history list -- no steps, no diffs."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    status: RunStatus
    agent_model: str
    max_steps: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None
    task: TaskRead


class RunDetail(RunSummary):
    steps: list[StepRead] = []
    diffs: list[DiffRead] = []


class RunCreated(BaseModel):
    id: uuid.UUID
    task_id: uuid.UUID
    status: RunStatus
