"""relation order

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-16

Relations were ordered by id (the order they were added); start from that.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('relations') as batch:
        batch.add_column(sa.Column('position', sa.Integer(), nullable=False,
                                   server_default='0'))
    op.execute("UPDATE relations SET position = id")


def downgrade() -> None:
    with op.batch_alter_table('relations') as batch:
        batch.drop_column('position')
