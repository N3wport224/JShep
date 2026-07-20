"""
Multi-channel outreach sequence configuration. A blueprint is an ordered
list of steps, each either `email` (handled by the existing follow-up
generation path) or `linkedin_view`/`linkedin_connection` (handed to
app.services.linkedin_automation). Pass `campaign_id` to configure a
specific campaign's sequence; omit it to configure the global default
sequence used by leads with no campaign (or whose campaign has no custom
blueprint) - see app.services.sequencing.get_sequence_steps.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import Campaign, SequenceStepConfig
from app.schemas import SequenceStepIn, SequenceStepOut

router = APIRouter(prefix="/sequence-steps", tags=["sequencing"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[SequenceStepOut])
async def get_sequence(campaign_id: str | None = None, db: AsyncSession = Depends(get_db)):
    query = select(SequenceStepConfig).order_by(SequenceStepConfig.step_number)
    query = query.where(SequenceStepConfig.campaign_id == campaign_id) if campaign_id else query.where(
        SequenceStepConfig.campaign_id.is_(None)
    )
    result = await db.execute(query)
    return result.scalars().all()


@router.put("", response_model=list[SequenceStepOut])
async def replace_sequence(steps: list[SequenceStepIn], campaign_id: str | None = None, db: AsyncSession = Depends(get_db)):
    """Replace the entire blueprint (global, or for one campaign) with the
    given ordered list of steps."""
    if campaign_id and not await db.get(Campaign, campaign_id):
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found")

    step_numbers = [s.step_number for s in steps]
    if len(step_numbers) != len(set(step_numbers)):
        raise HTTPException(status_code=422, detail="step_number values must be unique within a sequence")

    existing_query = select(SequenceStepConfig)
    existing_query = existing_query.where(SequenceStepConfig.campaign_id == campaign_id) if campaign_id else existing_query.where(
        SequenceStepConfig.campaign_id.is_(None)
    )
    existing = (await db.execute(existing_query)).scalars().all()
    for row in existing:
        await db.delete(row)
    await db.flush()

    created = []
    for step in steps:
        row = SequenceStepConfig(
            campaign_id=campaign_id, step_number=step.step_number, channel=step.channel, delay_days=step.delay_days
        )
        db.add(row)
        created.append(row)

    await db.commit()
    for row in created:
        await db.refresh(row)
    return sorted(created, key=lambda r: r.step_number)
