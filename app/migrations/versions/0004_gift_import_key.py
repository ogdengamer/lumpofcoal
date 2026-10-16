"""gift import key

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-16

Remembers which source photo (and recipient) an imported gift came from, so
running an import again never creates it twice.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0004'
down_revision: Union[str, None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('gifts') as batch:
        batch.add_column(sa.Column('import_key', sa.String(length=200), nullable=True))
    op.create_index('uq_gifts_import_key', 'gifts', ['import_key'], unique=True)


def downgrade() -> None:
    op.drop_index('uq_gifts_import_key', table_name='gifts')
    with op.batch_alter_table('gifts') as batch:
        batch.drop_column('import_key')
