"""add quote posts support (FEP-044f)

Adds the columns backing `activitypub.ap_object.Object.quote_ap_id` /
`quote_authorization_ap_id` on `inbox` and `outbox`, plus `outbox.quote_state`
and `outbox.quotes_count`. See PLAN-quote.md for the full design.

Autogenerate is not used here -- `Base.metadata` is empty at autogenerate
time in this project's `alembic/env.py`, so it would emit `drop_table` for
every existing table instead of a diff. Written by hand from the existing
migrations as a template.

`inbox.quote_ap_id` is indexed (plain `Index`, not an expression index) since
maintaining `outbox.quotes_count` queries it directly by value.

Revision ID: f4c2a7e8b910
Revises: a3f61c9d20b7
Create Date: 2026-08-20 13:00:00.000000+00:00

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "f4c2a7e8b910"
down_revision = "a3f61c9d20b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("inbox", sa.Column("quote_ap_id", sa.String(), nullable=True))
    op.add_column(
        "inbox", sa.Column("quote_authorization_ap_id", sa.String(), nullable=True)
    )
    op.add_column(
        "inbox",
        sa.Column(
            "quote_is_verified",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_index("ix_inbox_quote_ap_id", "inbox", ["quote_ap_id"])

    op.add_column("outbox", sa.Column("quote_ap_id", sa.String(), nullable=True))
    op.add_column(
        "outbox", sa.Column("quote_authorization_ap_id", sa.String(), nullable=True)
    )
    op.add_column("outbox", sa.Column("quote_state", sa.String(), nullable=True))
    op.add_column(
        "outbox",
        sa.Column(
            "quotes_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("outbox", "quotes_count")
    op.drop_column("outbox", "quote_state")
    op.drop_column("outbox", "quote_authorization_ap_id")
    op.drop_column("outbox", "quote_ap_id")

    op.drop_index("ix_inbox_quote_ap_id", table_name="inbox")
    op.drop_column("inbox", "quote_is_verified")
    op.drop_column("inbox", "quote_authorization_ap_id")
    op.drop_column("inbox", "quote_ap_id")
