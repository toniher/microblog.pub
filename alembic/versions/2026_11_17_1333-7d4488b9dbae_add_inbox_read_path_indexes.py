"""add inbox read-path indexes

There are no `ANALYZE` stats, so SQLite drives any inbox query carrying
`is_deleted IS 0` off `ix_inbox_stream` unless another index matches more
equality columns. That made the reply tree, DM threads, bookmarks,
favourites and the likes/shares of a note walk most of the inbox (27-172 ms
on a 197k-row inbox). Each index below wins for its query:

- `(conversation, is_deleted)` / `(ap_context, is_deleted)`: reply tree
  (`get_replies_tree`), one query per column. The first replaces
  `ix_inbox_conversation`, which is its prefix.
- `(visibility, ap_context)`: DM threads.
- `(is_bookmarked, is_deleted)`: bookmarks.
- `(liked_via_outbox_object_ap_id)`: favourites, forced with `INDEXED BY`.
- `(activity_object_ap_id, ap_type)`: likes/shares of one object. Replaces
  `ix_inbox_activity_object_ap_id`, which is its prefix.

Plain `op.create_index`/`op.drop_index`, not `batch_alter_table` -- rebuilding
`inbox` would silently drop its `in_reply_to` expression index and FTS5
triggers (see `f4c2a7e8b910`, same reasoning).

Revision ID: 7d4488b9dbae
Revises: 9c2d5e7a3f41
Create Date: 2026-11-17 13:33:00.000000+00:00

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "7d4488b9dbae"
down_revision = "9c2d5e7a3f41"
branch_labels = None
depends_on = None

_NEW_INDEXES = [
    ("ix_inbox_conversation_is_deleted", ["conversation", "is_deleted"]),
    ("ix_inbox_ap_context_is_deleted", ["ap_context", "is_deleted"]),
    ("ix_inbox_visibility_ap_context", ["visibility", "ap_context"]),
    ("ix_inbox_is_bookmarked_is_deleted", ["is_bookmarked", "is_deleted"]),
    ("ix_inbox_liked_via_outbox_object_ap_id", ["liked_via_outbox_object_ap_id"]),
    ("ix_inbox_activity_object_ap_id_ap_type", ["activity_object_ap_id", "ap_type"]),
]

# Superseded by a new composite index that starts with the same column.
_REPLACED_INDEXES = [
    ("ix_inbox_conversation", ["conversation"]),
    ("ix_inbox_activity_object_ap_id", ["activity_object_ap_id"]),
]


def upgrade() -> None:
    for name, columns in _NEW_INDEXES:
        op.create_index(name, "inbox", columns)
    for name, _ in _REPLACED_INDEXES:
        op.drop_index(name, table_name="inbox")


def downgrade() -> None:
    for name, columns in _REPLACED_INDEXES:
        op.create_index(name, "inbox", columns)
    for name, _ in _NEW_INDEXES:
        op.drop_index(name, table_name="inbox")
