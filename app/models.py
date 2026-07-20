"""ORM models for leads, outbound messages, replies, sender accounts, and
approval requests. Shared between the async engine (FastAPI, app/database.py)
and the sync engine (Celery workers + Alembic, app/db_sync.py)."""
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class LeadStatus(str, enum.Enum):
    NEW = "new"
    ENRICHED = "enriched"
    SENT = "sent"
    OPENED = "opened"
    REPLIED = "replied"
    BOUNCED = "bounced"
    OPTED_OUT = "opted_out"
    CLOSED_WON = "closed_won"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    contact_name: Mapped[str] = mapped_column(String, nullable=False)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    email: Mapped[str] = mapped_column(String, nullable=False, index=True)
    linkedin_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW)
    follow_up_step: Mapped[int] = mapped_column(Integer, default=0)
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    messages: Mapped[list["EmailMessage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin"
    )
    replies: Mapped[list["Reply"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin"
    )
    thread_messages: Mapped[list["ThreadMessage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin", order_by="ThreadMessage.created_at"
    )


class MessageDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(str, enum.Enum):
    DRAFT = "draft"
    QUEUED = "queued"
    SENT = "sent"
    OPENED = "opened"
    BOUNCED = "bounced"
    FAILED = "failed"


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
    sender_account_id: Mapped[str | None] = mapped_column(ForeignKey("sender_accounts.id"), nullable=True)
    direction: Mapped[MessageDirection] = mapped_column(Enum(MessageDirection), default=MessageDirection.OUTBOUND)
    sequence_step: Mapped[int] = mapped_column(Integer, default=0)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[MessageStatus] = mapped_column(Enum(MessageStatus), default=MessageStatus.DRAFT)
    tracking_id: Mapped[str] = mapped_column(String, default=_uuid, unique=True)
    message_id_header: Mapped[str | None] = mapped_column(String, nullable=True)
    in_reply_to_header: Mapped[str | None] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lead: Mapped["Lead"] = relationship(back_populates="messages", lazy="selectin")
    sender_account: Mapped["SenderAccount | None"] = relationship(lazy="selectin")


class Sentiment(str, enum.Enum):
    POSITIVE = "positive_interested"
    OBJECTION = "objection"
    NEGATIVE = "negative_opt_out"


class Reply(Base):
    __tablename__ = "replies"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
    raw_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    imap_uid: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    sentiment: Mapped[Sentiment | None] = mapped_column(Enum(Sentiment), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lead: Mapped["Lead"] = relationship(back_populates="replies", lazy="selectin")
    approval_request: Mapped["ApprovalRequest | None"] = relationship(
        back_populates="reply", uselist=False, lazy="selectin"
    )


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"


class ApprovalRequest(Base):
    """
    The human-in-the-loop gate. Created whenever a reply is classified as
    Positive/Interested. Outbound replies are NEVER sent automatically -
    execution pauses here until a human calls the approve/reject callback.

    `version` is used for optimistic-locking style re-checks; the resolving
    endpoint additionally takes a row lock (SELECT ... FOR UPDATE) so two
    concurrent approve/reject calls for the same request can't both succeed.
    """

    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
    reply_id: Mapped[str] = mapped_column(ForeignKey("replies.id"), nullable=False, unique=True)
    drafted_response: Mapped[str] = mapped_column(Text, nullable=False)
    edited_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ApprovalStatus] = mapped_column(Enum(ApprovalStatus), default=ApprovalStatus.PENDING)
    approval_token: Mapped[str] = mapped_column(String, default=_uuid, unique=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lead: Mapped["Lead"] = relationship(lazy="selectin")
    reply: Mapped["Reply"] = relationship(back_populates="approval_request", lazy="selectin")


class ThreadMessageRole(str, enum.Enum):
    PROSPECT = "prospect"
    SDR = "sdr"


class ThreadMessage(Base):
    """
    Lightweight per-lead conversation memory. Every inbound reply and every
    outbound send is appended here in order, independent of EmailMessage /
    Reply bookkeeping, so agentic prompts can be built from the *entire*
    thread history rather than just the single latest message.
    """

    __tablename__ = "thread_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
    role: Mapped[ThreadMessageRole] = mapped_column(Enum(ThreadMessageRole), nullable=False)
    subject: Mapped[str | None] = mapped_column(String, nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_email_message_id: Mapped[str | None] = mapped_column(ForeignKey("email_messages.id"), nullable=True)
    source_reply_id: Mapped[str | None] = mapped_column(ForeignKey("replies.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lead: Mapped["Lead"] = relationship(back_populates="thread_messages", lazy="selectin")


class SenderProvider(str, enum.Enum):
    SMTP = "smtp"
    INSTANTLY = "instantly"
    SMARTLEAD = "smartlead"


class SenderAccount(Base):
    """
    A pooled outbound sending identity. The SenderRotationManager picks a
    healthy, non-paused account for each send; bounce-rate tracking here
    auto-pauses accounts that cross an unsafe threshold to protect domain
    reputation.
    """

    __tablename__ = "sender_accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    provider: Mapped[SenderProvider] = mapped_column(Enum(SenderProvider), default=SenderProvider.SMTP)
    from_email: Mapped[str] = mapped_column(String, nullable=False)
    from_name: Mapped[str] = mapped_column(String, default="Sales Team")

    # SMTP-specific credentials (nullable for API-based providers)
    smtp_host: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    smtp_username: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_password: Mapped[str | None] = mapped_column(String, nullable=True)
    smtp_use_tls: Mapped[bool] = mapped_column(Boolean, default=True)

    # API-based provider credentials (Instantly / Smartlead)
    api_key: Mapped[str | None] = mapped_column(String, nullable=True)

    daily_limit: Mapped[int] = mapped_column(Integer, default=200)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    bounce_count: Mapped[int] = mapped_column(Integer, default=0)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def bounce_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return self.bounce_count / self.sent_count
