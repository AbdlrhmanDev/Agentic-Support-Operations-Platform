"""ticket intake channel and run quality score

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("channel", sa.String(length=20), server_default="api", nullable=False),
    )
    op.add_column("tickets", sa.Column("external_id", sa.String(length=200), nullable=True))
    op.create_unique_constraint(
        "uq_tickets_channel_external_id", "tickets", ["channel", "external_id"]
    )
    op.add_column(
        "agent_runs",
        sa.Column("quality", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "quality")
    op.drop_constraint("uq_tickets_channel_external_id", "tickets", type_="unique")
    op.drop_column("tickets", "external_id")
    op.drop_column("tickets", "channel")
