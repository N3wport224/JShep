"""Minimal local dashboard for reviewing/acting on pending approvals without
needing Telegram or Discord configured. Protected by the same admin auth as
other administrative routes (API key or JWT) - pass ?token=<jwt> if opening
directly in a browser without a way to set headers."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin
from app.database import get_db
from app.models import ApprovalRequest

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(require_admin)])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
async def dashboard_home(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(100))
    approvals = result.scalars().all()
    return templates.TemplateResponse("dashboard_list.html", {"request": request, "approvals": approvals})


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
async def dashboard_single(approval_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    approval = await db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return templates.TemplateResponse("dashboard_list.html", {"request": request, "approvals": [approval]})
