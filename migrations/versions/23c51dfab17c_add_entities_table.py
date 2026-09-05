"""add_entities_table

Revision ID: 23c51dfab17c
Revises: 1078fc83ad4e
Create Date: 2026-06-15 21:34:13.944245

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '23c51dfab17c'
down_revision: Union[str, Sequence[str], None] = '1078fc83ad4e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'entities',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('entity_type', sa.String(length=5), nullable=False, index=True),
        sa.Column('entity_uuid', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('ref', sa.String(length=20), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False, server_default='read'),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column('agent_name', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('entities')
