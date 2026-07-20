"""sender health guardian, warmup, multi-channel sequencing (LinkedIn touchpoints)

Revision ID: 0003_deliverability_multichannel
Revises: 0002_enterprise_modules
Create Date: 2026-08-03

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_deliverability_multichannel"
down_revision: Union[str, None] = "0002_enterprise_modules"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("sender_accounts") as batch_op:
        batch_op.add_column(sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("spam_complaint_count", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("warmup_stage", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(sa.Column("warmup_complete", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("counters_reset_at", sa.DateTime(), nullable=True))

    op.execute("UPDATE sender_accounts SET counters_reset_at = created_at WHERE counters_reset_at IS NULL")
    with op.batch_alter_table("sender_accounts") as batch_op:
        batch_op.alter_column("counters_reset_at", nullable=False)

    op.create_table(
        "sequence_step_configs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("campaign_id", sa.String(), sa.ForeignKey("campaigns.id"), nullable=True),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("email", "linkedin_view", "linkedin_connection", name="channeltype"),
            nullable=False,
            server_default="email",
        ),
        sa.Column("delay_days", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "linkedin_touchpoints",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("sequence_step", sa.Integer(), nullable=False),
        sa.Column(
            "action",
            sa.Enum("email", "linkedin_view", "linkedin_connection", name="channeltype"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "queued", "executed", "failed", name="touchpointstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("external_reference", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("executed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("linkedin_touchpoints")
    op.drop_table("sequence_step_configs")
    sa.Enum(name="touchpointstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="channeltype").drop(op.get_bind(), checkfirst=True)

    with op.batch_alter_table("sender_accounts") as batch_op:
        batch_op.drop_column("counters_reset_at")
        batch_op.drop_column("warmup_complete")
        batch_op.drop_column("warmup_stage")
        batch_op.drop_column("spam_complaint_count")
        batch_op.drop_column("open_count")
