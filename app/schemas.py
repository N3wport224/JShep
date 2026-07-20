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
