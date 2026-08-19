"""add inReplyTo expression indexes

`inReplyTo` exists only inside the `ap_object` JSON, so the reply lookups match
on `json_extract(ap_object, '$.inReplyTo')`. Without an index that is a full
scan of `inbox`/`outbox`, parsing every stored payload -- measured at ~73ms per
query over a 50k-row inbox. That cost was previously paid on write (recounting
replies when one arrives); serving the AP `replies` collection puts it on a
public read path, so index it.

SQLite only uses an expression index when the query renders the JSON path as a
literal rather than a bound parameter, which is what
`activitypub.models.in_reply_to_expr()` guarantees. Keep the two in sync.

Plain `op.create_index` rather than `batch_alter_table`: batch mode recreates
the table and reflects its indexes, and SQLite expression indexes do not
survive reflection.

Revision ID: a3f61c9d20b7
Revises: cc8f551f6765
Create Date: 2026-08-19 23:00:00.000000+00:00

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "a3f61c9d20b7"
down_revision = "cc8f551f6765"
branch_labels = None
depends_on = None

_IN_REPLY_TO = "json_extract(ap_object, '$.inReplyTo')"


def upgrade() -> None:
    op.create_index("ix_inbox_in_reply_to", "inbox", [sa.text(_IN_REPLY_TO)])
    op.create_index("ix_outbox_in_reply_to", "outbox", [sa.text(_IN_REPLY_TO)])


def downgrade() -> None:
    op.drop_index("ix_outbox_in_reply_to", table_name="outbox")
    op.drop_index("ix_inbox_in_reply_to", table_name="inbox")
