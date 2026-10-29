"""Print a short-lived native-local developer token; never for a deployed service."""

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from dotenv import load_dotenv

from habitquest_backend.config import Settings

load_dotenv()
settings = Settings()
if os.getenv("LOCAL_DEMO_AUTH") != "true" or not settings.jwt_secret:
    raise SystemExit("Native-local demo mode is not enabled. Use Supabase Auth instead.")
now = datetime.now(timezone.utc)
print(
    jwt.encode(
        {
            "sub": str(UUID("11111111-1111-1111-1111-111111111111")),
            "iat": now,
            "exp": now + timedelta(minutes=15),
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "role": "authenticated",
        },
        settings.jwt_secret.get_secret_value(),
        algorithm="HS256",
    )
)
