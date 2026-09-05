"""add locked_at to tasks

Revision ID: add_locked_at
Revises: 2e8f9b469eab
Create Date: 2026-06-08 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'add_locked_at'
down_revision: Union[str, Sequence[str], None] = '2e8f9b469eab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('tasks', sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('tasks', 'locked_at')
