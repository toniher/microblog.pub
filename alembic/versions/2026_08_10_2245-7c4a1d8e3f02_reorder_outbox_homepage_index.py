"""reorder outbox homepage index

Drop `visibility` from the leading position of `ix_outbox_homepage`. The
Mastodon outbox timeline query never constrains `visibility`, so SQLite could
not use the index at all there (it fell back to scanning
`ix_outbox_ap_published_at`). Leading with the two flags both the homepage and
the Mastodon timeline filter on lets one index serve both.

Revision ID: 7c4a1d8e3f02
Revises: 2e529833b5cb
Create Date: 2026-08-10 22:45:00.000000+00:00

"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '7c4a1d8e3f02'
down_revision = '2e529833b5cb'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.drop_index('ix_outbox_homepage')
        batch_op.create_index(
            'ix_outbox_homepage',
            ['is_deleted', 'is_hidden_from_homepage', 'ap_published_at'],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table('outbox', schema=None) as batch_op:
        batch_op.drop_index('ix_outbox_homepage')
        batch_op.create_index(
            'ix_outbox_homepage',
            ['visibility', 'is_deleted', 'is_hidden_from_homepage', 'ap_published_at'],
            unique=False,
        )
