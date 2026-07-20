"""
Human-in-the-loop notification dispatch. Fires an interactive notification
to whichever channels are configured (Telegram, Discord, and/or a generic
webhook) whenever an ApprovalRequest is created. Every channel links back
to the same token-protected approve/reject API endpoints - no channel can
approve/reject anything on its own; they only ever call our API.
"""
import logging

import httpx

from app.config import get_settings
from app.models import ApprovalRequest, Lead, Reply

logger = logging.getLogger(__name__)


def _truncate(text: str, limit: int = 500) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _approve_url(approval: ApprovalRequest) -> str:
    settings = get_settings()
    return f"{settings.api_base_url}/approvals/{approval.id}/decision?token={approval.approval_token}&action=approve"


def _reject_url(approval: ApprovalRequest) -> str:
    settings = get_settings()
    return f"{settings.api_base_url}/approvals/{approval.id}/decision?token={approval.approval_token}&action=reject"


def _dashboard_url(approval: ApprovalRequest) -> str:
    settings = get_settings()
    return f"{settings.api_base_url}/dashboard/approvals/{approval.id}"


def _notify_telegram(approval: ApprovalRequest, lead: Lead, reply: Reply) -> None:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return

    text = (
        f"*New interested reply — approval needed*\n\n"
        f"*Lead:* {lead.contact_name} ({lead.company_name})\n"
        f"*Their reply:*\n{_truncate(reply.raw_body)}\n\n"
        f"*AI drafted response:*\n{_truncate(approval.drafted_response)}"
    )
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": {
            "inline_keyboard": [
                [
                    {"text": "Approve & Send", "callback_data": f"approve:{approval.id}:{approval.approval_token}"},
                    {"text": "Reject / Take Over", "callback_data": f"reject:{approval.id}:{approval.approval_token}"},
                ]
            ]
        },
    }
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def _notify_discord(approval: ApprovalRequest, lead: Lead, reply: Reply) -> None:
    settings = get_settings()
    if not settings.discord_webhook_url:
        return

    content = (
        f"**New interested reply — approval needed**\n"
        f"**Lead:** {lead.contact_name} ({lead.company_name})\n\n"
        f"**Their reply:**\n{_truncate(reply.raw_body)}\n\n"
        f"**AI drafted response:**\n{_truncate(approval.drafted_response)}\n\n"
        f"[Approve & Send]({_approve_url(approval)}) | [Reject / Take Over]({_reject_url(approval)})"
    )
    resp = httpx.post(settings.discord_webhook_url, json={"content": content}, timeout=15)
    resp.raise_for_status()


def _notify_generic_webhook(approval: ApprovalRequest, lead: Lead, reply: Reply) -> None:
    settings = get_settings()
    if not settings.generic_notify_webhook_url:
        return

    payload = {
        "approval_id": approval.id,
        "lead": {
            "id": lead.id,
            "company_name": lead.company_name,
            "contact_name": lead.contact_name,
            "email": lead.email,
        },
        "prospect_reply": reply.raw_body,
        "ai_drafted_response": approval.drafted_response,
        "actions": {
            "approve_and_send": _approve_url(approval),
            "reject_take_over": _reject_url(approval),
        },
        "dashboard_url": _dashboard_url(approval),
    }
    resp = httpx.post(settings.generic_notify_webhook_url, json=payload, timeout=15)
    resp.raise_for_status()


def notify_approval_request(approval: ApprovalRequest, lead: Lead, reply: Reply) -> None:
    """Best-effort fan-out to every configured channel. Raises only if
    ALL configured channels fail, so a misconfigured channel doesn't
    silently swallow the only notification."""
    settings = get_settings()
    channels = [
        (settings.telegram_bot_token and settings.telegram_chat_id, _notify_telegram),
        (settings.discord_webhook_url, _notify_discord),
        (settings.generic_notify_webhook_url, _notify_generic_webhook),
    ]
    configured = [fn for enabled, fn in channels if enabled]
    if not configured:
        logger.warning(
            "No notification channel configured (Telegram/Discord/generic webhook). "
            "Approval %s is pending at %s",
            approval.id,
            _dashboard_url(approval),
        )
        return

    errors = []
    for fn in configured:
        try:
            fn(approval, lead, reply)
        except Exception as exc:  # noqa: BLE001 - fan-out must not abort on one channel's failure
            logger.error("Notification channel %s failed: %s", fn.__name__, exc)
            errors.append(exc)

    if len(errors) == len(configured):
        raise RuntimeError(f"All notification channels failed: {errors}")
