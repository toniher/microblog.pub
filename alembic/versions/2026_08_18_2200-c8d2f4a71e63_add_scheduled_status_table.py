"""add scheduled_status table

Revision ID: c8d2f4a71e63
Revises: 0a7b9bc9538e
Create Date: 2026-08-18 22:00:00.000000+00:00

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = 'c8d2f4a71e63'
down_revision = '0a7b9bc9538e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'scheduled_status',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('scheduled_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('tries', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_try', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_error', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('scheduled_status', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_scheduled_status_id'), ['id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_scheduled_status_scheduled_at'),
            ['scheduled_at'],
            unique=False,
        )
        # The worker's due-rows query filters on this column alone.
        batch_op.create_index(
            batch_op.f('ix_scheduled_status_next_try'), ['next_try'], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table('scheduled_status', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_scheduled_status_next_try'))
        batch_op.drop_index(batch_op.f('ix_scheduled_status_scheduled_at'))
        batch_op.drop_index(batch_op.f('ix_scheduled_status_id'))

    op.drop_table('scheduled_status')
