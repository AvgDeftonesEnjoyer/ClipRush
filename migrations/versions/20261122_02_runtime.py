"""Durable worker timestamps and emergency publication pause.

Revision ID: 20261122_02
Revises: 20261122_01
"""
from alembic import op
import sqlalchemy as sa

revision = "20261122_02"
down_revision = "20261122_01"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("recordings", "moments", "clips", "posts"):
        op.add_column(table, sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ))
    op.add_column("posts", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "runtime_controls",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_posts_account_sent_at", "posts", ["account_id", "sent_at"])


def downgrade():
    op.drop_index("ix_posts_account_sent_at", table_name="posts")
    op.drop_table("runtime_controls")
    op.drop_column("posts", "sent_at")
    for table in ("posts", "clips", "moments", "recordings"):
        op.drop_column(table, "updated_at")
