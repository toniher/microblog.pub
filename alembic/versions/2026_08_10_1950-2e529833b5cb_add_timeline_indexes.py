"""add timeline indexes

Revision ID: 2e529833b5cb
Revises: b3e7a1f0c9d4
Create Date: 2026-08-10 19:50:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '2e529833b5cb'
down_revision = 'b3e7a1f0c9d4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.create_index(
            'ix_inbox_ap_published_at', ['ap_published_at'], unique=False
        )
        batch_op.create_index(
            'ix_inbox_stream',
            ['is_deleted', 'is_hidden_from_stream', 'ap_published_at'],
            unique=False,
        )

    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.create_index(
            'ix_outbox_ap_published_at', ['ap_published_at'], unique=False
        )
        batch_op.create_index(
            'ix_outbox_homepage',
            ['visibility', 'is_deleted', 'is_hidden_from_homepage', 'ap_published_at'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.drop_index('ix_outbox_homepage')
        batch_op.drop_index('ix_outbox_ap_published_at')

    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.drop_index('ix_inbox_stream')
        batch_op.drop_index('ix_inbox_ap_published_at')
