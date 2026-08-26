"""add search_text columns and FTS5 trigram indexes

Search (`app/mastodon/router.py`'s `/api/v2/search`) matched `content`/
`preferredUsername`/`name` with a `LIKE ... ESCAPE` scan straight over
`ap_object`/`ap_actor` JSON (`f77ed8f`). That fixed the event-loop stalls
from filtering in Python, but left two gaps: SQLite's `LIKE`/`lower()` fold
ASCII case only, so `josé` didn't match an actor written `JOSÉ`; and with no
index, a rare-match query still read the whole box, decoding every row's
JSON before `LIMIT` could fill.

Normalizing (NFC + casefold, see `app/utils/search_text.py`) closes the
folding gap; doing it at *write* time rather than query time (a `casefold()`
UDF + `GLOB`) is also the faster of the two -- measured over 50k inbox rows
with a rare-match query: 99ms today, 263ms for the UDF approach (still a
scan), 1ms for a normalized column plus an FTS5 trigram index. One mechanism
closes both gaps.

`search_text` (defined on `Actor`/`InboxObject`/`OutboxObject` in
`activitypub/models.py`, alongside the mapper events that keep it in sync on
every future write) needs a one-time backfill here for existing rows --
normalization is Python, it can't be expressed in SQL. Batched via
`op.get_bind()`, following `b28c0551c236`'s precedent for a migration-time
Python data backfill.

Plain `op.add_column`, not `batch_alter_table`, for `inbox`/`outbox`: batch
mode reflects and recreates the table, and `ix_inbox_in_reply_to`/
`ix_outbox_in_reply_to` are expression indexes that do not survive
reflection (see `a3f61c9d20b7`/`f4c2a7e8b910`). Used for `actor` too, for
the same column-add, in one style across all three tables.

The FTS5 DDL is defined once, in `activitypub/models.py`
(`fts5_ddl_statements()`), and imported here rather than duplicated: that
module also attaches it to `after_create`/`before_drop` so
`tests/conftest.py`'s `Base.metadata.create_all` schema stays indexed too --
keeping the two single-sourced is what stops them drifting apart, the same
discipline `_IN_REPLY_TO_INDEX_EXPR` follows for the reply index. After the
backfill, `INSERT INTO <t>_search(<t>_search) VALUES('rebuild')` populates
each index from the now-filled column (the sync triggers only cover writes
made after they're created).

Measured cost: on a 27MB test DB, `search_text` plus its trigram index added
~38% to the file size. Worth re-checking against a real `data/` DB before
leaning on this further -- if the growth looks wrong there, the fallback is
the `casefold()` UDF alone: correct, 2.7x slower, and still a scan.

Revision ID: f2a8c4e91d67
Revises: b8f31a6c9e05
Create Date: 2026-08-24 23:00:00.000000+00:00

"""

import sqlalchemy as sa
from sqlalchemy.orm.session import Session

from alembic import op

# revision identifiers, used by Alembic.
revision = "f2a8c4e91d67"
down_revision = "b8f31a6c9e05"
branch_labels = None
depends_on = None

_TABLES = ("actor", "inbox", "outbox")
_BATCH_SIZE = 500


def upgrade() -> None:
    for table_name in _TABLES:
        op.add_column(table_name, sa.Column("search_text", sa.String(), nullable=True))

    _backfill_search_text()

    from activitypub.models import fts5_ddl_statements

    for table_name in _TABLES:
        create_stmts, _ = fts5_ddl_statements(table_name)
        for stmt in create_stmts:
            op.execute(stmt)
        # The sync triggers just created only cover writes from here on --
        # this populates the index for the rows the backfill above just set.
        op.execute(
            f"INSERT INTO {table_name}_search({table_name}_search) VALUES('rebuild')"
        )


def _backfill_search_text() -> None:
    from activitypub.models import Actor
    from activitypub.models import InboxObject
    from activitypub.models import OutboxObject
    from app.utils.search_text import actor_search_text
    from app.utils.search_text import object_search_text

    session = Session(bind=op.get_bind())
    try:
        for model, compute in (
            (Actor, lambda row: actor_search_text(row.ap_actor)),
            (InboxObject, lambda row: object_search_text(row.ap_object)),
            (OutboxObject, lambda row: object_search_text(row.ap_object)),
        ):
            last_id = 0
            while True:
                rows = (
                    session.query(model)
                    .filter(model.id > last_id)
                    .order_by(model.id)
                    .limit(_BATCH_SIZE)
                    .all()
                )
                if not rows:
                    break
                for row in rows:
                    row.search_text = compute(row)
                session.commit()
                last_id = rows[-1].id
    finally:
        session.close()


def downgrade() -> None:
    from activitypub.models import fts5_ddl_statements

    for table_name in _TABLES:
        _, drop_stmts = fts5_ddl_statements(table_name)
        for stmt in drop_stmts:
            op.execute(stmt)

    for table_name in _TABLES:
        op.drop_column(table_name, "search_text")
