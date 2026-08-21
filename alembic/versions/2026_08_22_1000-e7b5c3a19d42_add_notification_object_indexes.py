"""add notification object and hidden-announces indexes

Two indexes, both for queries added with the follow-options/notification-types
work.

1. `notifications.outbox_object_id` / `inbox_object_id`. SQLite does not
   auto-index FK columns, and `notifications` was left out of `b05a3893306a`
   because nothing queried these as a predicate -- they were only followed as
   relationships from an already-known row. The `poll`-ended sweep
   (`app.poll_notifications`) changes that: it finds work with a `NOT EXISTS`
   correlated on them, every few seconds inside the outgoing-activity worker.
   Unindexed, each candidate poll cost a full scan of `notifications` --
   measured at 2.9ms over a 30k-row table and linear in it from there; 0.02ms
   with the index.

2. `actor.are_announces_hidden_from_stream`. `announces_hidden_actor_ids()`
   runs as a subquery on every timeline read (and every streaming-pump tick).
   Unindexed, SQLite built a transient AUTOMATIC PARTIAL COVERING INDEX over
   the whole actor table on each execution: +0.25ms per timeline query over
   5k actors, against +0.006ms with the index.

Plain (non-partial) indexes throughout, per the note on `b05a3893306a`: a
partial index would be silently unused here.

Revision ID: e7b5c3a19d42
Revises: d4e1f6a2b837
Create Date: 2026-08-22 10:00:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "e7b5c3a19d42"
down_revision = "d4e1f6a2b837"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_notifications_outbox_object_id"),
            ["outbox_object_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_notifications_inbox_object_id"),
            ["inbox_object_id"],
            unique=False,
        )

    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_actor_are_announces_hidden_from_stream"),
            ["are_announces_hidden_from_stream"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_actor_are_announces_hidden_from_stream"))

    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_notifications_inbox_object_id"))
        batch_op.drop_index(batch_op.f("ix_notifications_outbox_object_id"))
