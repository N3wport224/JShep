"""
Calendar scheduling helper. Deliberately minimal: CALENDAR_BOOKING_URL
points at a Cal.com event type or a Google Calendar appointment schedule
link, either of which already handles slot availability/timezones/booking
confirmation on its own - this service just decides *when* to offer it
(app.services.llm.detect_meeting_intent) and hands the URL to the agentic
drafting prompt so it gets included in the reply.
"""
from app.config import get_settings


def get_booking_link() -> str | None:
    return get_settings().calendar_booking_url
