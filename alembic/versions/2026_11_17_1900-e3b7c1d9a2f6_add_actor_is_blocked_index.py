"""add actor is_blocked index

`/admin/blocks` and Mastodon's `/api/v1/blocks` filter on `is_blocked IS 1`.
Without an index each view scanned every actor and sorted it (46 ms on an
81k-actor prod copy); with it the lookup is a sub-millisecond index search.
Plain, not partial (partial indexes on `IS 0/1` are ignored here), and no
query filters on `is_blocked IS 0`, so the planner can't misuse it for the
"everyone" side.

Plain `op.create_index`, not `batch_alter_table`: no table rebuild needed.

Revision ID: e3b7c1d9a2f6
Revises: 7d4488b9dbae
Create Date: 2026-11-17 19:00:00.000000+00:00

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "e3b7c1d9a2f6"
down_revision = "7d4488b9dbae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_actor_is_blocked", "actor", ["is_blocked"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_actor_is_blocked", table_name="actor")
