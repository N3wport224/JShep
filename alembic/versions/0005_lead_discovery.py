"""automated lead discovery: Lead.source + discovery_runs audit table

Revision ID: 0005_lead_discovery
Revises: 0004_spam_guardian
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_lead_discovery"
down_revision: Union[str, None] = "0004_spam_guardian"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("leads") as batch_op:
        batch_op.add_column(sa.Column("source", sa.String(), nullable=False, server_default="manual"))

    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("campaign_id", sa.String(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("campaign_name", sa.String(), nullable=False),
        sa.Column("search_query", sa.String(), nullable=False),
        sa.Column("businesses_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leads_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suppressed_skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("no_contact_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum("success", "failed", name="discoveryrunstatus"),
            nullable=False,
            server_default="success",
        ),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("discovery_runs")
    sa.Enum(name="discoveryrunstatus").drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("leads") as batch_op:
        batch_op.drop_column("source")
