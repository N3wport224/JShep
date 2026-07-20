"""ORM models for leads, outbound messages, replies, and approval requests."""
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

    messages: Mapped[list["EmailMessage"]] = relationship(back_populates="lead", cascade="all, delete-orphan")
    replies: Mapped[list["Reply"]] = relationship(back_populates="lead", cascade="all, delete-orphan")


class MessageDirection(str, enum.Enum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class MessageStatus(str, enum.Enum):
    DRAFT = "draft"
    SENT = "sent"
    OPENED = "opened"
    BOUNCED = "bounced"
    FAILED = "failed"


class EmailMessage(Base):
    __tablename__ = "email_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
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

    lead: Mapped["Lead"] = relationship(back_populates="messages")


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

    lead: Mapped["Lead"] = relationship(back_populates="replies")
    approval_request: Mapped["ApprovalRequest | None"] = relationship(back_populates="reply", uselist=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    lead: Mapped["Lead"] = relationship()
    reply: Mapped["Reply"] = relationship(back_populates="approval_request")
