"""add inbox actor_id+ap_published_at composite index

Backs `app.mastodon.timelines.fetch_inbox_timeline_page`'s
`force_actor_index=True` path (used by `GET /api/v1/timelines/list/{id}`):
without it SQLite drives a list timeline off `ix_inbox_stream` and evaluates
membership as a post-filter, which is an O(inbox size) scan for a quiet or
empty list. The `INDEXED BY` hint that path uses only works once this index
exists.

Plain `op.create_index`/`op.drop_index`, not `batch_alter_table` -- rebuilding
`inbox` would silently drop its `in_reply_to` expression index and FTS5
triggers (see `f4c2a7e8b910`, same reasoning).

Revision ID: 9c2d5e7a3f41
Revises: 17a4a33f1e12
Create Date: 2026-09-05 15:00:00.000000+00:00

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "9c2d5e7a3f41"
down_revision = "17a4a33f1e12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_inbox_actor_id_ap_published_at",
        "inbox",
        ["actor_id", "ap_published_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_actor_id_ap_published_at", table_name="inbox")
