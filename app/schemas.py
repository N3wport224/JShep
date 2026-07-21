"""Pydantic request/response schemas."""
import enum
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.models import (
    ApprovalStatus,
    ChannelType,
    DiscoveryRunStatus,
    LeadStatus,
    MessageStatus,
    SenderProvider,
    Sentiment,
    SuppressionSource,
    TouchpointStatus,
)


class SpamRiskLevel(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SpamScoreResult(BaseModel):
    """Output of app.services.spam_guardian.score_email - a deterministic
    heuristic pass, not an LLM call, run on every cold email/follow-up
    before it can enter the outbound sending queue."""

    score: int = Field(ge=0, le=100)
    risk_level: SpamRiskLevel
    trigger_words_found: list[str] = Field(default_factory=list)
    caps_ratio: float = 0.0
    link_count: int = 0
    link_density: float = 0.0
    reasons: list[str] = Field(default_factory=list)


class LeadIn(BaseModel):
    company_name: str
    contact_name: str
    website: Optional[str] = None
    email: EmailStr
    linkedin_url: Optional[str] = None


class LeadUploadPayload(BaseModel):
    leads: list[LeadIn] = Field(default_factory=list)
    campaign_id: Optional[str] = None


class LeadOut(BaseModel):
    id: str
    company_name: str
    contact_name: str
    website: Optional[str]
    email: str
    linkedin_url: Optional[str]
    status: LeadStatus
    follow_up_step: int
    next_follow_up_at: Optional[datetime]
    campaign_id: Optional[str]
    variant_id: Optional[str]
    crm_contact_id: Optional[str]
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailMessageOut(BaseModel):
    id: str
    lead_id: str
    direction: str
    sequence_step: int
    subject: str
    body: str
    status: MessageStatus
    spam_score: Optional[int]
    spam_flagged: bool
    spam_reasons: Optional[str]
    sent_at: Optional[datetime]
    opened_at: Optional[datetime]

    model_config = {"from_attributes": True}


class ReviewApprovalIn(BaseModel):
    """Optional edited copy when clearing a NEEDS_REVIEW message back to
    DRAFT so it can be sent - if omitted, the original AI draft is kept."""

    subject: Optional[str] = None
    body: Optional[str] = None


class ReplyOut(BaseModel):
    id: str
    lead_id: str
    raw_subject: Optional[str]
    raw_body: str
    sentiment: Optional[Sentiment]
    received_at: datetime

    model_config = {"from_attributes": True}


class ApprovalRequestOut(BaseModel):
    id: str
    lead_id: str
    reply_id: str
    drafted_response: str
    edited_response: Optional[str]
    status: ApprovalStatus
    created_at: datetime

    model_config = {"from_attributes": True}


class ApprovalDecision(BaseModel):
    edited_response: Optional[str] = None


# ---------------------------------------------------------------------------
# Structured LLM output schemas. Every agentic LLM call in app.services.llm
# validates its JSON response against one of these before accepting it; a
# ValidationError triggers an automatic self-correction retry that feeds the
# error back to the model rather than surfacing malformed data to callers.
# ---------------------------------------------------------------------------


class ColdEmailDraft(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class SentimentClassification(BaseModel):
    sentiment: Sentiment
    reasoning: str = Field(default="", max_length=500)


class ReplyDraft(BaseModel):
    draft: str = Field(min_length=1, max_length=4000)
    reasoning: str = Field(default="", max_length=500)


class ReplyCritique(BaseModel):
    approved: bool
    revised_draft: Optional[str] = Field(default=None, max_length=4000)
    critique: str = Field(default="", max_length=500)


class MeetingIntent(BaseModel):
    wants_to_book: bool
    reasoning: str = Field(default="", max_length=300)


# ---------------------------------------------------------------------------
# Suppression list
# ---------------------------------------------------------------------------


class SuppressionEntryIn(BaseModel):
    email: EmailStr
    reason: Optional[str] = None


class SuppressionEntryOut(BaseModel):
    id: str
    email: str
    domain: str
    reason: Optional[str]
    source: SuppressionSource
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# A/B campaign testing
# ---------------------------------------------------------------------------


class CampaignVariantIn(BaseModel):
    label: str = Field(min_length=1, max_length=50)
    prompt_hint: Optional[str] = Field(default=None, max_length=1000)
    weight: int = Field(default=1, ge=1)


class CampaignIn(BaseModel):
    name: str
    description: Optional[str] = None
    variants: list[CampaignVariantIn] = Field(min_length=2)


class CampaignVariantOut(BaseModel):
    id: str
    label: str
    prompt_hint: Optional[str]
    weight: int
    sent_count: int
    open_count: int
    reply_count: int
    positive_count: int
    open_rate: float
    reply_rate: float
    positive_rate: float

    model_config = {"from_attributes": True}


class CampaignOut(BaseModel):
    id: str
    name: str
    description: Optional[str]
    created_at: datetime
    variants: list[CampaignVariantOut]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Sender health guardian
# ---------------------------------------------------------------------------


class SenderAccountOut(BaseModel):
    id: str
    name: str
    provider: SenderProvider
    is_paused: bool
    pause_reason: Optional[str]
    daily_limit: int
    sent_count: int
    bounce_count: int
    open_count: int
    spam_complaint_count: int
    bounce_rate: float
    open_rate: float
    spam_complaint_rate: float
    warmup_stage: int
    warmup_complete: bool

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Multi-channel sequencing (email + LinkedIn)
# ---------------------------------------------------------------------------


class SequenceStepIn(BaseModel):
    step_number: int = Field(ge=1)
    channel: ChannelType = ChannelType.EMAIL
    delay_days: int = Field(ge=0)


class SequenceStepOut(BaseModel):
    id: str
    campaign_id: Optional[str]
    step_number: int
    channel: ChannelType
    delay_days: int

    model_config = {"from_attributes": True}


class LinkedInTouchpointOut(BaseModel):
    id: str
    lead_id: str
    sequence_step: int
    action: ChannelType
    status: TouchpointStatus
    external_reference: Optional[str]
    error: Optional[str]
    scheduled_at: datetime
    executed_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Automated lead discovery
# ---------------------------------------------------------------------------


class DiscoveryTriggerIn(BaseModel):
    campaign_name: Optional[str] = None


class DiscoveryRunOut(BaseModel):
    id: str
    campaign_id: Optional[str]
    campaign_name: str
    search_query: str
    businesses_found: int
    leads_created: int
    duplicates_skipped: int
    suppressed_skipped: int
    no_contact_found: int
    status: DiscoveryRunStatus
    error: Optional[str]
    created_at: datetime

    model_config = {"from_attributes": True}
