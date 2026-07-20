"""Pydantic request/response schemas."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field

from app.models import ApprovalStatus, LeadStatus, MessageStatus, Sentiment


class LeadIn(BaseModel):
    company_name: str
    contact_name: str
    website: Optional[str] = None
    email: EmailStr
    linkedin_url: Optional[str] = None


class LeadUploadPayload(BaseModel):
    leads: list[LeadIn] = Field(default_factory=list)


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
    sent_at: Optional[datetime]
    opened_at: Optional[datetime]

    model_config = {"from_attributes": True}


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
