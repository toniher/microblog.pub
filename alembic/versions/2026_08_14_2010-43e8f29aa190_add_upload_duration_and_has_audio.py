"""add upload duration and has_audio

Revision ID: 43e8f29aa190
Revises: b05a3893306a
Create Date: 2026-08-14 20:10:00.000000+00:00

"""
import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "43e8f29aa190"
down_revision = "b05a3893306a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("upload", schema=None) as batch_op:
        batch_op.add_column(sa.Column("duration", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("has_audio", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("upload", schema=None) as batch_op:
        batch_op.drop_column("has_audio")
        batch_op.drop_column("duration")
