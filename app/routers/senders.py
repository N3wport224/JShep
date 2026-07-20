"""Sender health guardian admin API: view per-account health (volume,
bounce/open/spam-complaint rates, warmup progress) and manually
pause/unpause an account. Auto-pause happens reactively in
app.services.sender_rotation and app.routers.tracking; this is the human
override surface (e.g. re-enabling an account after investigating why it
tripped)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import SenderAccount
from app.schemas import SenderAccountOut

router = APIRouter(prefix="/senders", tags=["senders"], dependencies=[Depends(require_admin)])


@router.get("", response_model=list[SenderAccountOut])
async def list_senders(db: AsyncSession = Depends(get_db)):
    from sqlalchemy import select

    result = await db.execute(select(SenderAccount).order_by(SenderAccount.name))
    return result.scalars().all()


@router.post("/{sender_id}/pause", response_model=SenderAccountOut)
async def pause_sender(sender_id: str, reason: str = "Manually paused", db: AsyncSession = Depends(get_db)):
    account = await db.get(SenderAccount, sender_id)
    if not account:
        raise HTTPException(status_code=404, detail="Sender account not found")
    account.is_paused = True
    account.pause_reason = reason
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{sender_id}/unpause", response_model=SenderAccountOut)
async def unpause_sender(sender_id: str, db: AsyncSession = Depends(get_db)):
    """Manual re-enable only - auto-pause never clears itself, since a
    bounce/spam-complaint rate crossing 2%/0.1% warrants a human looking
    at *why* before resuming sends from that identity."""
    account = await db.get(SenderAccount, sender_id)
    if not account:
        raise HTTPException(status_code=404, detail="Sender account not found")
    account.is_paused = False
    account.pause_reason = None
    await db.commit()
    await db.refresh(account)
    return account
