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

    # CAN-SPAM/GDPR: every lead gets a stable, unguessable one-click
    # unsubscribe token at creation time (see routers.suppression).
    unsubscribe_token: Mapped[str] = mapped_column(String, default=_uuid, unique=True)

    # CRM sync (see app.services.crm) - set once a positive-sentiment lead
    # has been pushed to the configured CRM.
    crm_contact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    crm_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # A/B campaign assignment (see app.services.campaigns) - a lead is
    # assigned one variant for its whole lifecycle so metrics stay clean.
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    variant_id: Mapped[str | None] = mapped_column(ForeignKey("campaign_variants.id"), nullable=True)

    messages: Mapped[list["EmailMessage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin"
    )
    replies: Mapped[list["Reply"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin"
    )
    thread_messages: Mapped[list["ThreadMessage"]] = relationship(
        back_populates="lead", cascade="all, delete-orphan", lazy="selectin", order_by="ThreadMessage.created_at"
    )
    campaign: Mapped["Campaign | None"] = relationship(lazy="selectin")
    variant: Mapped["CampaignVariant | None"] = relationship(lazy="selectin")


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
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    spam_complaint_count: Mapped[int] = mapped_column(Integer, default=0)
    is_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Inbox warmup: daily_limit ramps up over app.services.warmup's
    # configured stages instead of starting at full volume on day one -
    # this is what actually protects deliverability for a brand-new sending
    # identity, on top of the reactive bounce/spam-complaint circuit breaker.
    warmup_stage: Mapped[int] = mapped_column(Integer, default=0)
    warmup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    counters_reset_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    @property
    def bounce_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return self.bounce_count / self.sent_count

    @property
    def open_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return self.open_count / self.sent_count

    @property
    def spam_complaint_rate(self) -> float:
        if self.sent_count == 0:
            return 0.0
        return self.spam_complaint_count / self.sent_count


class SuppressionSource(str, enum.Enum):
    OPT_OUT_REPLY = "opt_out_reply"
    UNSUBSCRIBE_LINK = "unsubscribe_link"
    BOUNCE = "bounce"
    MANUAL = "manual"


class SuppressionEntry(Base):
    """
    Global do-not-contact list (CAN-SPAM/GDPR compliance). Checked before
    every outbound send (cold email, follow-up, or approved reply) - see
    app.services.suppression.is_suppressed. An entry suppresses both the
    exact email AND its domain, so once a hard opt-out/bounce is recorded,
    no lead at that domain can ever be targeted again by accident.
    """

    __tablename__ = "suppression_entries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    domain: Mapped[str] = mapped_column(String, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[SuppressionSource] = mapped_column(Enum(SuppressionSource), default=SuppressionSource.MANUAL)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Campaign(Base):
    """An outbound campaign that can run multiple A/B email variants across
    its assigned leads. See app.services.campaigns for variant assignment
    and app.core.metrics for the Prometheus counters kept in sync with the
    per-variant columns below."""

    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    variants: Mapped[list["CampaignVariant"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", lazy="selectin", order_by="CampaignVariant.label"
    )


class CampaignVariant(Base):
    """One A/B variant (e.g. "A" / "B") within a Campaign. `prompt_hint` is
    injected into the cold-email generation prompt so each variant gets a
    distinct hook/angle/subject-line style. Counters here are the source of
    truth for per-variant performance; app.core.metrics mirrors them as
    Prometheus counters for dashboards/alerting."""

    __tablename__ = "campaign_variants"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(ForeignKey("campaigns.id"), nullable=False)
    label: Mapped[str] = mapped_column(String, nullable=False)
    prompt_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    weight: Mapped[int] = mapped_column(Integer, default=1)

    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_count: Mapped[int] = mapped_column(Integer, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    campaign: Mapped["Campaign"] = relationship(back_populates="variants", lazy="selectin")

    @property
    def open_rate(self) -> float:
        return self.open_count / self.sent_count if self.sent_count else 0.0

    @property
    def reply_rate(self) -> float:
        return self.reply_count / self.sent_count if self.sent_count else 0.0

    @property
    def positive_rate(self) -> float:
        return self.positive_count / self.sent_count if self.sent_count else 0.0


class ChannelType(str, enum.Enum):
    EMAIL = "email"
    LINKEDIN_VIEW = "linkedin_view"
    LINKEDIN_CONNECTION = "linkedin_connection"


class SequenceStepConfig(Base):
    """
    A multi-channel outreach sequence blueprint: an ordered list of steps,
    each either an email send (handled by the existing follow-up email
    generation path) or a LinkedIn touchpoint (handled by
    app.services.linkedin_automation). `campaign_id` NULL means this step
    applies to every lead not otherwise assigned a campaign-specific
    sequence - see app.tasks.celery_tasks.follow_up_sequence_task.
    """

    __tablename__ = "sequence_step_configs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    step_number: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[ChannelType] = mapped_column(Enum(ChannelType), default=ChannelType.EMAIL)
    delay_days: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class TouchpointStatus(str, enum.Enum):
    PENDING = "pending"
    QUEUED = "queued"
    EXECUTED = "executed"
    FAILED = "failed"


class LinkedInTouchpoint(Base):
    """
    One scheduled/executed LinkedIn action (profile view or connection
    request) for a lead, as part of its multi-channel sequence. This never
    touches LinkedIn directly - app.services.linkedin_automation formats and
    ships a payload to an external headless-browser automation layer
    (PhantomBuster, a local Playwright worker, etc.) configured via
    LINKEDIN_AUTOMATION_WEBHOOK_URL; with no webhook configured, execution
    is safely simulated (logged, marked executed) so sequences don't stall
    in dev/test.
    """

    __tablename__ = "linkedin_touchpoints"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    lead_id: Mapped[str] = mapped_column(ForeignKey("leads.id"), nullable=False)
    sequence_step: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[ChannelType] = mapped_column(Enum(ChannelType), nullable=False)
    status: Mapped[TouchpointStatus] = mapped_column(Enum(TouchpointStatus), default=TouchpointStatus.PENDING)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    lead: Mapped["Lead"] = relationship(lazy="selectin")
