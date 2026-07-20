"""A/B testing campaigns: define a campaign with 2+ email variants (each
with its own prompt hint influencing the LLM's subject-line/hook style),
assign leads to it, and read back per-variant performance."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import Campaign, CampaignVariant
from app.schemas import CampaignIn, CampaignOut

router = APIRouter(prefix="/campaigns", tags=["campaigns"], dependencies=[Depends(require_admin)])


@router.post("", response_model=CampaignOut)
async def create_campaign(payload: CampaignIn, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(Campaign).where(Campaign.name == payload.name))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail=f"Campaign {payload.name!r} already exists")

    campaign = Campaign(name=payload.name, description=payload.description)
    db.add(campaign)
    await db.flush()

    for variant_in in payload.variants:
        db.add(
            CampaignVariant(
                campaign_id=campaign.id,
                label=variant_in.label,
                prompt_hint=variant_in.prompt_hint,
                weight=variant_in.weight,
            )
        )

    await db.commit()
    await db.refresh(campaign)
    return campaign


@router.get("", response_model=list[CampaignOut])
async def list_campaigns(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Campaign).order_by(Campaign.created_at.desc()))
    return result.scalars().all()


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: str, db: AsyncSession = Depends(get_db)):
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("/{campaign_id}/stats", response_model=CampaignOut)
async def campaign_stats(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Alias for GET /campaigns/{id} - the per-variant open/reply/positive
    rates are already embedded in each variant, this just names the intent
    clearly for analytics dashboards."""
    campaign = await db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign
