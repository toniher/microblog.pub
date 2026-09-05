"""add mastodon_list and mastodon_list_member tables

Revision ID: 17a4a33f1e12
Revises: 0dcf9e09fd18
Create Date: 2026-09-04 21:16:48.000000+00:00

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "17a4a33f1e12"
down_revision = "0dcf9e09fd18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mastodon_list",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("replies_policy", sa.String(), nullable=False, server_default="list"),
        sa.Column("exclusive", sa.Boolean(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("mastodon_list", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_mastodon_list_id"), ["id"], unique=False)

    op.create_table(
        "mastodon_list_member",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("list_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["mastodon_list.id"],
            name="fk_mastodon_list_member_list_id",
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["actor.id"],
            name="fk_mastodon_list_member_actor_id",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("mastodon_list_member", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_mastodon_list_member_id"), ["id"], unique=False
        )
        # A single named unique index rather than a table-level UNIQUE
        # constraint plus a separate lookup index: one B-tree doubling as
        # both the "already a member" 422 check and the leading-column
        # (list_id) lookup `list_member_actor_ids()` runs -- same reasoning
        # as `0dcf9e09fd18`'s outbox alias index.
        batch_op.create_index(
            batch_op.f("uix_mastodon_list_member"),
            ["list_id", "actor_id"],
            unique=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("mastodon_list_member", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("uix_mastodon_list_member"))
        batch_op.drop_index(batch_op.f("ix_mastodon_list_member_id"))
    op.drop_table("mastodon_list_member")

    with op.batch_alter_table("mastodon_list", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_mastodon_list_id"))
    op.drop_table("mastodon_list")
