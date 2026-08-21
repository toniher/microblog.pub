"""add actor is_muted index

`muted_actor_ids()` runs as a subquery on every timeline *and* notification
read (`not_from_muted_actors()` renders it twice per timeline query -- once
for the actor, once nested for boosts of a muted actor's posts), plus the
notification-side callers: the notifications list/unread-count endpoints, the
streaming pump, the push worker's per-tick fetch, and the admin notification
badge rendered on every admin page. `4c9a1f7b2e58` added `is_muted` (and
`muted_until`, `are_notifications_muted`) without indexing any of them.

Unindexed, SQLite paid for an AUTOMATIC PARTIAL COVERING INDEX build over the
whole actor table plus a bare SCAN actor on every execution -- the same shape
`e7b5c3a19d42` fixed for `are_announces_hidden_from_stream`, which is why this
one was worth doing now rather than leaving it as pre-existing. Measured over
5k actors (1% muted), 50k inbox rows, 120 interleaved runs: 0.386ms -> 0.178ms
median per timeline query, both scans gone.

Composite shapes (`is_muted, muted_until` / `+ are_notifications_muted`) were
measured and rejected -- within noise of the single column, since muted
actors are ~1% of rows and the seek on `is_muted` alone already narrows to a
handful. Plain, not partial, per the note on `b05a3893306a`.

Revision ID: b8f31a6c9e05
Revises: e7b5c3a19d42
Create Date: 2026-08-22 14:00:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "b8f31a6c9e05"
down_revision = "e7b5c3a19d42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_actor_is_muted"),
            ["is_muted"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_actor_is_muted"))
