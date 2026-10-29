"""Native-local developer tokens shared by local_token.py and demo.py; never for a deployed service."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt

from habitquest_backend.config import Settings

PARENT = UUID("11111111-1111-1111-1111-111111111111")
EDITOR = UUID("e0000000-0000-0000-0000-0000000000e1")
# A second editor: reviewers must differ from authors and publishers from release builders.
REVIEWER = UUID("e0000000-0000-0000-0000-0000000000e2")


def demo_settings() -> Settings:
    settings = Settings()
    if os.getenv("LOCAL_DEMO_AUTH") != "true" or not settings.jwt_secret:
        raise SystemExit(
            "Native-local demo mode is not enabled (LOCAL_DEMO_AUTH=true and JWT_SECRET in .env). "
            "For Supabase, sign in through local Auth instead."
        )
    return settings


def local_token(settings: Settings, sub: UUID, *, editor: bool = False, minutes: int = 15) -> str:
    """A short-lived HS256 token; `editor` adds the admin-assigned `app_metadata.hq_role` claim."""
    if settings.jwt_secret is None:
        raise ValueError("JWT_SECRET is required for native-local tokens")
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(sub),
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "role": "authenticated",
    }
    if editor:
        claims["app_metadata"] = {"hq_role": "editor"}
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")
