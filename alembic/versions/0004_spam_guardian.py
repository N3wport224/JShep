"""pre-send spam guardian: EmailMessage scoring columns + needs_review status

Revision ID: 0004_spam_guardian
Revises: 0003_deliverability_multichannel
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_spam_guardian"
down_revision: Union[str, None] = "0003_deliverability_multichannel"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_STATUSES = ("draft", "queued", "sent", "opened", "bounced", "failed")
_NEW_STATUSES = _OLD_STATUSES + ("needs_review",)


def upgrade() -> None:
    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.add_column(sa.Column("spam_score", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("spam_flagged", sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column("spam_reasons", sa.Text(), nullable=True))

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE messagestatus ADD VALUE IF NOT EXISTS 'needs_review'")
    # SQLite (and the ORM's Enum(..., native_enum=False) fallback) stores
    # status as a plain string column with no DB-level CHECK to update.


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Postgres can't drop a single enum value in place; any NEEDS_REVIEW
        # rows must be reassigned before downgrading, or this will fail with
        # a foreign-key-style constraint error on the enum type - by design,
        # since silently reinterpreting them would be worse.
        op.execute("UPDATE email_messages SET status = 'failed' WHERE status = 'needs_review'")

    with op.batch_alter_table("email_messages") as batch_op:
        batch_op.drop_column("spam_reasons")
        batch_op.drop_column("spam_flagged")
        batch_op.drop_column("spam_score")
