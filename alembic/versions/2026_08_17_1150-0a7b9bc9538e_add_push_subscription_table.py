"""add push_subscription table

Revision ID: 0a7b9bc9538e
Revises: 43e8f29aa190
Create Date: 2026-08-17 11:50:00.000000+00:00

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "0a7b9bc9538e"
down_revision = "43e8f29aa190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscription",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("access_token_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("p256dh", sa.String(), nullable=False),
        sa.Column("auth", sa.String(), nullable=False),
        sa.Column("alert_mention", sa.Boolean(), nullable=False),
        sa.Column("alert_status", sa.Boolean(), nullable=False),
        sa.Column("alert_reblog", sa.Boolean(), nullable=False),
        sa.Column("alert_follow", sa.Boolean(), nullable=False),
        sa.Column("alert_follow_request", sa.Boolean(), nullable=False),
        sa.Column("alert_favourite", sa.Boolean(), nullable=False),
        sa.Column("alert_poll", sa.Boolean(), nullable=False),
        sa.Column("alert_update", sa.Boolean(), nullable=False),
        sa.Column("policy", sa.String(), nullable=False),
        sa.Column("last_notification_id", sa.Integer(), nullable=False),
        sa.Column("tries", sa.Integer(), nullable=False),
        sa.Column("next_try", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_try", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["access_token_id"],
            ["indieauth_access_token.id"],
            name="fk_push_subscription_access_token_id",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("access_token_id"),
    )
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_push_subscription_access_token_id"),
            ["access_token_id"],
            unique=True,
        )
        batch_op.create_index(
            batch_op.f("ix_push_subscription_id"), ["id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_push_subscription_next_try"), ["next_try"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("push_subscription", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_push_subscription_next_try"))
        batch_op.drop_index(batch_op.f("ix_push_subscription_id"))
        batch_op.drop_index(batch_op.f("ix_push_subscription_access_token_id"))

    op.drop_table("push_subscription")
