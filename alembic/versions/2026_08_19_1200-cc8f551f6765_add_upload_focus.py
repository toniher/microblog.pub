"""add upload focus

Revision ID: cc8f551f6765
Revises: c8d2f4a71e63
Create Date: 2026-08-19 12:00:00.000000+00:00

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "cc8f551f6765"
down_revision = "c8d2f4a71e63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upload", schema=None) as batch_op:
        batch_op.add_column(sa.Column("focus_x", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("focus_y", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("upload", schema=None) as batch_op:
        batch_op.drop_column("focus_y")
        batch_op.drop_column("focus_x")
