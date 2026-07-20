"""enterprise modules: suppression list, campaigns/variants, lead CRM+unsubscribe fields

Revision ID: 0002_enterprise_modules
Revises: 0001_initial
Create Date: 2026-07-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_enterprise_modules"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "suppression_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(), nullable=False, unique=True),
        sa.Column("domain", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column(
            "source",
            sa.Enum("opt_out_reply", "unsubscribe_link", "bounce", "manual", name="suppressionsource"),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_suppression_entries_email", "suppression_entries", ["email"])
    op.create_index("ix_suppression_entries_domain", "suppression_entries", ["domain"])

    op.create_table(
        "campaigns",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False, unique=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    op.create_table(
        "campaign_variants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("campaign_id", sa.String(), sa.ForeignKey("campaigns.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("prompt_hint", sa.Text(), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reply_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # batch_alter_table (rather than plain op.add_column with an inline
    # ForeignKey) so this migration also works on SQLite, which can't ALTER
    # a table to add a constraint in place - batch mode uses a copy-and-move
    # strategy there while still doing a normal ALTER TABLE on Postgres.
    with op.batch_alter_table("leads") as batch_op:
        batch_op.add_column(sa.Column("unsubscribe_token", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("crm_contact_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("crm_synced_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("campaign_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("variant_id", sa.String(), nullable=True))
        batch_op.create_foreign_key("fk_leads_campaign_id", "campaigns", ["campaign_id"], ["id"])
        batch_op.create_foreign_key("fk_leads_variant_id", "campaign_variants", ["variant_id"], ["id"])

    # Backfill unsubscribe_token for any pre-existing rows, then enforce
    # NOT NULL + uniqueness the same way the ORM model declares it.
    op.execute("UPDATE leads SET unsubscribe_token = id WHERE unsubscribe_token IS NULL")
    with op.batch_alter_table("leads") as batch_op:
        batch_op.alter_column("unsubscribe_token", nullable=False)
        batch_op.create_unique_constraint("uq_leads_unsubscribe_token", ["unsubscribe_token"])


def downgrade() -> None:
    with op.batch_alter_table("leads") as batch_op:
        batch_op.drop_constraint("uq_leads_unsubscribe_token", type_="unique")
        batch_op.drop_constraint("fk_leads_variant_id", type_="foreignkey")
        batch_op.drop_constraint("fk_leads_campaign_id", type_="foreignkey")
        batch_op.drop_column("variant_id")
        batch_op.drop_column("campaign_id")
        batch_op.drop_column("crm_synced_at")
        batch_op.drop_column("crm_contact_id")
        batch_op.drop_column("unsubscribe_token")

    op.drop_table("campaign_variants")
    op.drop_table("campaigns")

    op.drop_index("ix_suppression_entries_domain", table_name="suppression_entries")
    op.drop_index("ix_suppression_entries_email", table_name="suppression_entries")
    op.drop_table("suppression_entries")
    sa.Enum(name="suppressionsource").drop(op.get_bind(), checkfirst=True)
