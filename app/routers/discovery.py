"""Automated lead discovery admin API: trigger an on-demand discovery run
(the same pipeline Celery beat runs daily, see app.tasks.celery_tasks.
discover_leads_task) and inspect run history/results."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import DiscoveryRun
from app.schemas import DiscoveryRunOut, DiscoveryTriggerIn
from app.tasks.celery_tasks import discover_leads_task
from app.tasks.dispatch import enqueue

router = APIRouter(prefix="/discovery", tags=["discovery"], dependencies=[Depends(require_admin)])


@router.post("/run", status_code=202)
async def trigger_discovery(payload: DiscoveryTriggerIn = DiscoveryTriggerIn()):
    """Run discovery now instead of waiting for the daily schedule. Pass
    campaign_name to run just that campaign's configured search; omit it to
    run every campaign configured in LEAD_DISCOVERY_CAMPAIGNS_JSON. Always
    runs regardless of LEAD_DISCOVERY_ENABLED - an explicit admin trigger
    isn't silently skipped by the opt-in flag the way the daily scheduled
    run is."""
    task_id = enqueue(discover_leads_task, payload.campaign_name, force=True)
    return {"task_id": task_id, "status": "queued"}


@router.get("/runs", response_model=list[DiscoveryRunOut])
async def list_discovery_runs(campaign_name: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(DiscoveryRun)
    if campaign_name:
        query = query.where(DiscoveryRun.campaign_name == campaign_name)
    result = await db.execute(query.order_by(DiscoveryRun.created_at.desc()).limit(100))
    return result.scalars().all()
