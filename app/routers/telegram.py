"""Receives Telegram callback_query events from the [Approve & Send] /
[Reject] inline buttons sent by app/services/notifier.py."""
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import get_settings
from app.core.security import limiter
from app.database import get_db
from app.routers.approvals import resolve_decision

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
@limiter.limit(get_settings().rate_limit_webhook)
async def telegram_webhook(request: Request, db=Depends(get_db)):
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
    try:
        await resolve_decision(db, approval_id, token, action)
        _answer_callback(callback["id"], f"{action.title()}d")
    except HTTPException as exc:
        logger.error("Failed to execute Telegram approval decision: %s", exc.detail)
        _answer_callback(callback["id"], f"Error: {exc.detail}")
    except Exception as exc:  # noqa: BLE001 - always ack the callback, never raise into Telegram
        logger.error("Failed to execute Telegram approval decision: %s", exc)
        _answer_callback(callback["id"], f"Error: {exc}")

    return {"ok": True}
