"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-07-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sender_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column(
            "provider",
            sa.Enum("smtp", "instantly", "smartlead", name="senderprovider"),
            nullable=False,
            server_default="smtp",
        ),
        sa.Column("from_email", sa.String(), nullable=False),
        sa.Column("from_name", sa.String(), nullable=False, server_default="Sales Team"),
        sa.Column("smtp_host", sa.String(), nullable=True),
        sa.Column("smtp_port", sa.Integer(), nullable=True),
        sa.Column("smtp_username", sa.String(), nullable=True),
        sa.Column("smtp_password", sa.String(), nullable=True),
        sa.Column("smtp_use_tls", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("api_key", sa.String(), nullable=True),
        sa.Column("daily_limit", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bounce_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("pause_reason", sa.String(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("company_name", sa.String(), nullable=False),
        sa.Column("contact_name", sa.String(), nullable=False),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("linkedin_url", sa.String(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new", "enriched", "sent", "opened", "replied", "bounced", "opted_out", "closed_won",
                name="leadstatus",
            ),
            nullable=False,
            server_default="new",
        ),
        sa.Column("follow_up_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_follow_up_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_leads_email", "leads", ["email"])

    op.create_table(
        "email_messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("sender_account_id", sa.String(), sa.ForeignKey("sender_accounts.id"), nullable=True),
        sa.Column(
            "direction", sa.Enum("outbound", "inbound", name="messagedirection"),
            nullable=False, server_default="outbound",
        ),
        sa.Column("sequence_step", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "queued", "sent", "opened", "bounced", "failed", name="messagestatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("tracking_id", sa.String(), nullable=False, unique=True),
        sa.Column("message_id_header", sa.String(), nullable=True),
        sa.Column("in_reply_to_header", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "replies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("raw_subject", sa.String(), nullable=True),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column("imap_uid", sa.String(), nullable=True, unique=True),
        sa.Column(
            "sentiment",
            sa.Enum("positive_interested", "objection", "negative_opt_out", name="sentiment"),
            nullable=True,
        ),
        sa.Column("received_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "approval_requests",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("reply_id", sa.String(), sa.ForeignKey("replies.id"), nullable=False, unique=True),
        sa.Column("drafted_response", sa.Text(), nullable=False),
        sa.Column("edited_response", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "approved", "rejected", "sent", name="approvalstatus"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("approval_token", sa.String(), nullable=False, unique=True),
        sa.Column("notified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )

    op.create_table(
        "thread_messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lead_id", sa.String(), sa.ForeignKey("leads.id"), nullable=False),
        sa.Column("role", sa.Enum("prospect", "sdr", name="threadmessagerole"), nullable=False),
        sa.Column("subject", sa.String(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("source_email_message_id", sa.String(), sa.ForeignKey("email_messages.id"), nullable=True),
        sa.Column("source_reply_id", sa.String(), sa.ForeignKey("replies.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("thread_messages")
    op.drop_table("approval_requests")
    op.drop_table("replies")
    op.drop_table("email_messages")
    op.drop_index("ix_leads_email", table_name="leads")
    op.drop_table("leads")
    op.drop_table("sender_accounts")
    sa.Enum(name="threadmessagerole").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="approvalstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="sentiment").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="messagestatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="messagedirection").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="leadstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="senderprovider").drop(op.get_bind(), checkfirst=True)
