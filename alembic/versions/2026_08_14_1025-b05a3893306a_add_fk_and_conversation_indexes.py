"""add fk and conversation indexes

SQLite does not auto-index FK columns. `inbox.conversation` is the
load-bearing one: every notification query runs
`InboxObject.conversation.in_(select(MutedConversation.conversation))`
inside a `NOT IN` subquery, and the `MutedConversation` side is already
indexed while the `inbox` side was not. The rest are the remaining
unindexed foreign keys on `inbox`, `outbox` and `outgoing_activity`.

Plain (non-partial) indexes throughout -- see the partial-index note on
`5eabb060f447`: SQLAlchemy's `.is_(False)` renders `IS 0`, and SQLite's
implication test against a `= 0` index predicate is textual, so a partial
index here would be silently unused.

Revision ID: b05a3893306a
Revises: 5eabb060f447
Create Date: 2026-08-14 08:25:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = 'b05a3893306a'
down_revision = '5eabb060f447'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_inbox_actor_id'), ['actor_id'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_inbox_conversation'), ['conversation'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_inbox_relates_to_inbox_object_id'),
            ['relates_to_inbox_object_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_inbox_relates_to_outbox_object_id'),
            ['relates_to_outbox_object_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_inbox_undone_by_inbox_object_id'),
            ['undone_by_inbox_object_id'],
            unique=False,
        )

    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_outbox_conversation'), ['conversation'], unique=False
        )
        batch_op.create_index(
            batch_op.f('ix_outbox_relates_to_inbox_object_id'),
            ['relates_to_inbox_object_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_outbox_relates_to_outbox_object_id'),
            ['relates_to_outbox_object_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_outbox_relates_to_actor_id'),
            ['relates_to_actor_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_outbox_undone_by_outbox_object_id'),
            ['undone_by_outbox_object_id'],
            unique=False,
        )

    with op.batch_alter_table('outgoing_activity', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_outgoing_activity_outbox_object_id'),
            ['outbox_object_id'],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f('ix_outgoing_activity_inbox_object_id'),
            ['inbox_object_id'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('outgoing_activity', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outgoing_activity_inbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_outgoing_activity_outbox_object_id'))

    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_outbox_undone_by_outbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_outbox_relates_to_actor_id'))
        batch_op.drop_index(batch_op.f('ix_outbox_relates_to_outbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_outbox_relates_to_inbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_outbox_conversation'))

    with op.batch_alter_table('inbox', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_inbox_undone_by_inbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_inbox_relates_to_outbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_inbox_relates_to_inbox_object_id'))
        batch_op.drop_index(batch_op.f('ix_inbox_conversation'))
        batch_op.drop_index(batch_op.f('ix_inbox_actor_id'))
