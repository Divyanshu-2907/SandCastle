"""add stop_reason to runs

`status` records only whether the run threw. A run that exhausted its step
budget raises nothing, so it is stored as COMPLETED while having changed no
files -- indistinguishable, in the database, from one that finished the task.
This column carries the agent loop's own reason for stopping so the two can be
told apart. Nullable: runs that predate it have no recorded reason, and
back-filling one would be inventing it.

Revision ID: 7aa7b66d2d07
Revises: 9bdff430eaaa
Create Date: 2026-11-20 07:19:34.876319

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7aa7b66d2d07'
down_revision: Union[str, None] = '9bdff430eaaa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('runs', sa.Column('stop_reason', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'stop_reason')
