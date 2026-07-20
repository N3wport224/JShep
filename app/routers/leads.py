"""Lead ingestion (CSV/JSON) and LLM-based enrichment (cold email generation).
Enrichment is always queued to Celery - the request path never blocks on an
LLM call."""
import csv
import io
import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import limiter, require_admin
from app.database import get_db
from app.models import Campaign, Lead, LeadStatus, LinkedInTouchpoint
from app.schemas import LeadIn, LeadOut, LeadUploadPayload, LinkedInTouchpointOut
from app.tasks.celery_tasks import batch_enrich_leads_task, enrich_lead_task
from app.tasks.dispatch import enqueue

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


async def _persist_leads(leads_in: list[LeadIn], db: AsyncSession, campaign_id: str | None = None) -> list[Lead]:
    if not leads_in:
        raise HTTPException(status_code=422, detail="No leads found in payload")

    if campaign_id and not await db.get(Campaign, campaign_id):
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found")

    created: list[Lead] = []
    for lead_in in leads_in:
        existing = (await db.execute(select(Lead).where(Lead.email.ilike(lead_in.email)))).scalar_one_or_none()
        if existing:
            continue
        lead = Lead(**lead_in.model_dump(), campaign_id=campaign_id)
        db.add(lead)
        created.append(lead)
    await db.commit()
    for lead in created:
        await db.refresh(lead)
    return created


@router.post("/upload", response_model=list[LeadOut])
async def upload_leads_json(payload: LeadUploadPayload, db: AsyncSession = Depends(get_db)):
    """Accept a JSON body {"leads": [...], "campaign_id": "..."} of target leads.
    campaign_id (optional) enrolls every lead into that A/B test campaign."""
    return await _persist_leads(payload.leads, db, campaign_id=payload.campaign_id)


@router.post("/upload/file", response_model=list[LeadOut])
async def upload_leads_file(
    file: UploadFile = File(...), campaign_id: str | None = None, db: AsyncSession = Depends(get_db)
):
    """Accept a CSV or JSON file upload of target leads. Pass ?campaign_id=
    to enroll every lead into that A/B test campaign."""
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
    return await _persist_leads(leads_in, db, campaign_id=campaign_id)


@router.get("", response_model=list[LeadOut])
async def list_leads(status: LeadStatus | None = None, db: AsyncSession = Depends(get_db)):
    query = select(Lead)
    if status:
        query = query.where(Lead.status == status)
    result = await db.execute(query.order_by(Lead.created_at.desc()))
    return result.scalars().all()


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: str, db: AsyncSession = Depends(get_db)):
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


@router.get("/{lead_id}/linkedin-touchpoints", response_model=list[LinkedInTouchpointOut])
async def list_linkedin_touchpoints(lead_id: str, db: AsyncSession = Depends(get_db)):
    if not await db.get(Lead, lead_id):
        raise HTTPException(status_code=404, detail="Lead not found")
    result = await db.execute(
        select(LinkedInTouchpoint)
        .where(LinkedInTouchpoint.lead_id == lead_id)
        .order_by(LinkedInTouchpoint.sequence_step)
    )
    return result.scalars().all()


@router.post("/{lead_id}/enrich", status_code=202)
@limiter.limit("30/minute")
async def enrich_lead(request: Request, lead_id: str, db: AsyncSession = Depends(get_db)):
    """Queue LLM-based cold email generation for a lead (async - returns a task ID)."""
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    task_id = enqueue(enrich_lead_task, lead.id)
    return {"lead_id": lead.id, "task_id": task_id, "status": "queued"}


@router.post("/enrich/batch", status_code=202, dependencies=[Depends(require_admin)])
@limiter.limit("10/minute")
async def enrich_leads_batch(request: Request, status: LeadStatus = LeadStatus.NEW, db: AsyncSession = Depends(get_db)):
    """Queue batch enrichment for every lead currently in `status` (default: new)."""
    result = await db.execute(select(Lead.id).where(Lead.status == status))
    lead_ids = [row[0] for row in result.all()]
    if not lead_ids:
        return {"queued": 0, "task_id": None}
    task_id = enqueue(batch_enrich_leads_task, lead_ids)
    return {"queued": len(lead_ids), "task_id": task_id}
