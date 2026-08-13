"""add activity queue indexes

Both `outgoing_activity` and `incoming_activity` had no index besides the
`id` PK, yet the worker polls
`WHERE next_try <= now AND is_errored = 0 AND is_sent/is_processed = 0
ORDER BY next_try` every 2 seconds forever. Lead with the two boolean flags,
not `next_try`: `app/prune.py` keeps errored rows forever and the success
path never nulls `next_try`, so almost every row satisfies
`next_try <= now()` — a `next_try`-leading index would range-scan the whole
history instead of seeking into the small pending partition.

Revision ID: 5eabb060f447
Revises: 7c4a1d8e3f02
Create Date: 2026-08-13 15:00:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '5eabb060f447'
down_revision = '7c4a1d8e3f02'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('outgoing_activity', schema=None) as batch_op:
        batch_op.create_index(
            'ix_outgoing_activity_queue',
            ['is_errored', 'is_sent', 'next_try'],
            unique=False,
        )

    with op.batch_alter_table('incoming_activity', schema=None) as batch_op:
        batch_op.create_index(
            'ix_incoming_activity_queue',
            ['is_errored', 'is_processed', 'next_try'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('incoming_activity', schema=None) as batch_op:
        batch_op.drop_index('ix_incoming_activity_queue')

    with op.batch_alter_table('outgoing_activity', schema=None) as batch_op:
        batch_op.drop_index('ix_outgoing_activity_queue')
