"""Receives Telegram callback_query events from the [Approve & Send] /
[Reject] inline buttons sent by app/services/notifier.py."""
import logging

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import ApprovalRequest
from app.routers.approvals import _execute_decision

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/telegram", tags=["telegram"])


def _answer_callback(callback_query_id: str, text: str) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token:
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/answerCallbackQuery"
    try:
        httpx.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except httpx.HTTPError as exc:
        logger.warning("Failed to answer Telegram callback query: %s", exc)


@router.post("/webhook")
async def telegram_webhook(request: Request, db: Session = Depends(get_db)):
    update = await request.json()
    callback = update.get("callback_query")
    if not callback:
        return {"ok": True}

    data = callback.get("data", "")
    parts = data.split(":")
    if len(parts) != 3:
        _answer_callback(callback["id"], "Malformed action")
        return {"ok": True}

    action, approval_id, token = parts
    approval = db.get(ApprovalRequest, approval_id)
    if not approval:
        _answer_callback(callback["id"], "Approval request not found")
        return {"ok": True}

    try:
        _execute_decision(db, approval, token, action)
        _answer_callback(callback["id"], f"{action.title()}d")
    except Exception as exc:  # noqa: BLE001 - always ack the callback, never raise into Telegram
        logger.error("Failed to execute Telegram approval decision: %s", exc)
        _answer_callback(callback["id"], f"Error: {exc}")

    return {"ok": True}
