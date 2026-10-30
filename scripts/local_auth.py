"""Native-local developer tokens shared by local_token.py and demo.py; never for a deployed service."""

import os
from uuid import UUID

from habitquest_backend.auth import EDITOR, PARENT, REVIEWER, issue_token
from habitquest_backend.config import Settings

__all__ = ["EDITOR", "PARENT", "REVIEWER", "demo_settings", "local_token"]


def demo_settings() -> Settings:
    settings = Settings()
    if os.getenv("LOCAL_DEMO_AUTH") != "true" or not settings.jwt_secret:
        raise SystemExit(
            "Native-local demo mode is not enabled (LOCAL_DEMO_AUTH=true and JWT_SECRET in .env). "
            "For a deployed service with DEV_LOGIN, log in with DEV_LOGIN_PASSWORD instead."
        )
    return settings


def local_token(settings: Settings, sub: UUID, *, editor: bool = False, minutes: int = 15) -> str:
    """A short-lived HS256 token; `editor` adds the admin-assigned `app_metadata.hq_role` claim."""
    return issue_token(settings, sub, editor=editor, minutes=minutes)
