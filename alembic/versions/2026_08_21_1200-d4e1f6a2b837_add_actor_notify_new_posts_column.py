"""add actor notify new posts column

Revision ID: d4e1f6a2b837
Revises: f4c2a7e8b910
Create Date: 2026-08-21 12:00:00.000000+00:00

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "d4e1f6a2b837"
down_revision = "f4c2a7e8b910"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "are_new_posts_notified",
                sa.Boolean(),
                server_default="0",
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("actor", schema=None) as batch_op:
        batch_op.drop_column("are_new_posts_notified")
