"""Ordered status and order links

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD = "status IN ('idea', 'bought', 'wrapped', 'given')"
NEW = "status IN ('idea', 'ordered', 'bought', 'wrapped', 'given')"


def upgrade() -> None:
    with op.batch_alter_table('gifts', recreate='always') as batch:
        batch.drop_constraint('valid_status', type_='check')
        batch.create_check_constraint('valid_status', NEW)
        batch.add_column(sa.Column('order_url', sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.execute("UPDATE gifts SET status = 'bought' WHERE status = 'ordered'")
    with op.batch_alter_table('gifts', recreate='always') as batch:
        batch.drop_column('order_url')
        batch.drop_constraint('valid_status', type_='check')
        batch.create_check_constraint('valid_status', OLD)
