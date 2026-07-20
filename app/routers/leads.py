"""Lead ingestion (CSV/JSON) and LLM-based enrichment (cold email generation)."""
import csv
import io
import logging

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import EmailMessage, Lead, LeadStatus, MessageDirection, MessageStatus
from app.schemas import LeadIn, LeadOut, LeadUploadPayload
from app.services.llm import get_llm_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/leads", tags=["leads"])

REQUIRED_CSV_COLUMNS = {"company_name", "contact_name", "email"}


def _parse_csv(raw: bytes) -> list[LeadIn]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or not REQUIRED_CSV_COLUMNS.issubset({f.strip() for f in reader.fieldnames}):
        raise HTTPException(
            status_code=422,
            detail=f"CSV must include columns: {sorted(REQUIRED_CSV_COLUMNS)} (optional: website, linkedin_url)",
        )
    leads = []
    for row_num, row in enumerate(reader, start=2):
        try:
            leads.append(
                LeadIn(
                    company_name=row.get("company_name", "").strip(),
                    contact_name=row.get("contact_name", "").strip(),
                    website=(row.get("website") or "").strip() or None,
                    email=row.get("email", "").strip(),
                    linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                )
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=f"Row {row_num} invalid: {exc}") from exc
    return leads


def _persist_leads(leads_in: list[LeadIn], db: Session) -> list[Lead]:
    if not leads_in:
        raise HTTPException(status_code=422, detail="No leads found in payload")

    created: list[Lead] = []
    for lead_in in leads_in:
        existing = db.query(Lead).filter(Lead.email.ilike(lead_in.email)).first()
        if existing:
            continue
        lead = Lead(**lead_in.model_dump())
        db.add(lead)
        created.append(lead)
    db.commit()
    for lead in created:
        db.refresh(lead)
    return created


@router.post("/upload", response_model=list[LeadOut])
def upload_leads_json(payload: LeadUploadPayload, db: Session = Depends(get_db)):
    """Accept a JSON body {"leads": [...]} of target leads."""
    return _persist_leads(payload.leads, db)


@router.post("/upload/file", response_model=list[LeadOut])
async def upload_leads_file(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Accept a CSV or JSON file upload of target leads."""
    raw = await file.read()
    if file.filename and file.filename.lower().endswith(".json"):
        import json

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=422, detail=f"Invalid JSON file: {exc}") from exc
        leads_in = LeadUploadPayload(leads=data.get("leads", data if isinstance(data, list) else [])).leads
    else:
        leads_in = _parse_csv(raw)
    return _persist_leads(leads_in, db)


@router.get("", response_model=list[LeadOut])
def list_leads(status: LeadStatus | None = None, db: Session = Depends(get_db)):
    query = db.query(Lead)
    if status:
        query = query.filter(Lead.status == status)
    return query.order_by(Lead.created_at.desc()).all()


@router.get("/{lead_id}", response_model=LeadOut)
def get_lead(lead_id: str, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.post("/{lead_id}/enrich")
def enrich_lead(lead_id: str, db: Session = Depends(get_db)):
    """Generate a personalized cold email draft for a lead via the LLM."""
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    llm = get_llm_client()
    try:
        generated = llm.generate_cold_email(
            {
                "company_name": lead.company_name,
                "contact_name": lead.contact_name,
                "website": lead.website,
                "linkedin_url": lead.linkedin_url,
            }
        )
    except Exception as exc:  # noqa: BLE001 - malformed JSON, rate limits, or provider errors
        logger.error("Enrichment failed for lead %s: %s", lead_id, exc)
        raise HTTPException(status_code=502, detail=f"LLM failed to produce email copy: {exc}") from exc

    message = EmailMessage(
        lead_id=lead.id,
        direction=MessageDirection.OUTBOUND,
        sequence_step=0,
        subject=generated["subject"],
        body=generated["body"],
        status=MessageStatus.DRAFT,
    )
    db.add(message)
    lead.status = LeadStatus.ENRICHED
    db.commit()
    db.refresh(message)
    return {
        "message_id": message.id,
        "lead_id": lead.id,
        "subject": message.subject,
        "body": message.body,
    }
