"""Add outbox alias column

Human-readable alias overriding an outbox object's permalink -- see the
`url` property on `OutboxObject`. No backfill: existing rows keep their
current `url` until an alias is set.

Deliberately NOT `op.batch_alter_table`, unlike the `slug` column this
mirrors (`b28c0551c236`). Batch mode rebuilds the table (create/copy/drop/
rename), and on today's `outbox` that rebuild is destructive twice over:

  * `ix_outbox_in_reply_to` (`a3f61c9d20b7`) is an expression index, which
    SQLAlchemy cannot reflect -- it warns "Skipped unsupported reflection of
    expression-based index" and simply does not recreate it, silently turning
    every reply lookup back into a full table scan.
  * The `outbox_search_ai/ad/au` FTS5 triggers (`f2a8c4e91d67`) are attached
    to the old table and vanish with it, so the search index stops tracking
    new posts. `SELECT count(*) FROM outbox_search` still looks healthy
    afterwards -- it reads through to the content table -- while `MATCH`
    quietly returns nothing for anything written since.

A plain `ADD COLUMN` is a native SQLite ALTER that touches no existing row,
and a `CREATE UNIQUE INDEX` gives the uniqueness a table-level UNIQUE
constraint would have (SQLite allows unlimited NULLs under it, so unaliased
rows are unaffected) while doubling as the lookup index -- one B-tree rather
than the two a separate index + constraint would build.

Revision ID: 0dcf9e09fd18
Revises: f2a8c4e91d67
Create Date: 2026-08-28 23:00:00.000000+00:00

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0dcf9e09fd18"
down_revision = "f2a8c4e91d67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox", sa.Column("alias", sa.String(), nullable=True))
    op.create_index("ix_outbox_alias", "outbox", ["alias"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_outbox_alias", table_name="outbox")
    op.drop_column("outbox", "alias")
