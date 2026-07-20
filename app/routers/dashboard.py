"""Minimal local dashboard for reviewing/acting on pending approvals without
needing Telegram or Discord configured."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import ApprovalRequest

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


@router.get("", response_class=HTMLResponse)
def dashboard_home(request: Request, db: Session = Depends(get_db)):
    approvals = db.query(ApprovalRequest).order_by(ApprovalRequest.created_at.desc()).limit(100).all()
    return templates.TemplateResponse("dashboard_list.html", {"request": request, "approvals": approvals})


@router.get("/approvals/{approval_id}", response_class=HTMLResponse)
def dashboard_single(approval_id: str, request: Request, db: Session = Depends(get_db)):
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval request not found")
    return templates.TemplateResponse("dashboard_list.html", {"request": request, "approvals": [approval]})
