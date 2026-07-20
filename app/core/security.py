"""
Authentication for administrative/dashboard routes: an API key header
(X-API-Key) or a JWT bearer token issued via POST /auth/token. Either is
accepted so a human clicking dashboard links and a service calling the API
programmatically both have a supported path.

If ADMIN_API_KEY / (ADMIN_USERNAME+ADMIN_PASSWORD) aren't configured, auth is
left open for local development - app.main logs a loud warning at startup
so this can't silently ship unauthenticated.
"""
import hmac
import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Header, HTTPException, Query
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)


def auth_is_configured() -> bool:
    settings = get_settings()
    return bool(settings.admin_api_key or (settings.admin_username and settings.admin_password))


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _verify_jwt(token: str) -> bool:
    settings = get_settings()
    try:
        jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return True
    except jwt.PyJWTError:
        return False


async def require_admin(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    """Dependency for admin/dashboard routes. Accepts, in order:
    X-API-Key header, Authorization: Bearer <jwt>, or a ?token= query param
    (so dashboard links/emails can carry auth without a header)."""
    settings = get_settings()
    if not auth_is_configured():
        return

    if settings.admin_api_key and x_api_key and hmac.compare_digest(x_api_key, settings.admin_api_key):
        return

    bearer = None
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(" ", 1)[1]
    bearer = bearer or token
    if bearer and _verify_jwt(bearer):
        return

    raise HTTPException(status_code=401, detail="Missing or invalid credentials (X-API-Key or Bearer token)")
