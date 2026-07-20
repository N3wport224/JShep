"""Issues short-lived JWTs for dashboard/admin access, checked against
ADMIN_USERNAME/ADMIN_PASSWORD. X-API-Key remains available as a simpler
alternative for service-to-service calls (see app.core.security)."""
import hmac

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.core.security import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


class TokenRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


@router.post("/token", response_model=TokenResponse)
def issue_token(payload: TokenRequest):
    settings = get_settings()
    if not settings.admin_username or not settings.admin_password:
        raise HTTPException(status_code=503, detail="Password auth is not configured (ADMIN_USERNAME/ADMIN_PASSWORD)")

    valid = hmac.compare_digest(payload.username, settings.admin_username) and hmac.compare_digest(
        payload.password, settings.admin_password
    )
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return TokenResponse(
        access_token=create_access_token(payload.username), expires_in_minutes=settings.jwt_expire_minutes
    )
