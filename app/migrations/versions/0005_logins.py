"""password logins: sessions and invites

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-16

For AUTH_MODE=password. Cloudflare mode doesn't use these tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('people') as batch:
        batch.add_column(sa.Column('password_hash', sa.String(length=200), nullable=True))
    op.create_table('login_sessions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'))
    op.create_index('ix_login_sessions_person_id', 'login_sessions', ['person_id'])
    op.create_table('invites',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('person_id', sa.Integer(), nullable=False),
        sa.Column('created_by_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['created_by_id'], ['people.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'))
    op.create_index('ix_invites_person_id', 'invites', ['person_id'])


def downgrade() -> None:
    op.drop_index('ix_invites_person_id', table_name='invites')
    op.drop_table('invites')
    op.drop_index('ix_login_sessions_person_id', table_name='login_sessions')
    op.drop_table('login_sessions')
    with op.batch_alter_table('people') as batch:
        batch.drop_column('password_hash')
