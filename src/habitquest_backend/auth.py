import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

from habitquest_backend.config import Settings

# The fixed development parent; `parent:<label>` gives further parents with stable ids.
PARENT = UUID("11111111-1111-1111-1111-111111111111")
TOKEN_URL = "/v1/auth/token"


def verify_token(token: str, settings: Settings) -> UUID:
    """Verify one of our HS256 access tokens and return its subject (hq.users.id)."""
    try:
        if settings.jwt_secret is None:
            raise ValueError("JWT_SECRET is not configured")
        claims = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iat"]},
        )
        if claims.get("role") != "authenticated":
            raise ValueError("Parent authentication required")
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired token") from exc


def issue_token(settings: Settings, sub: UUID, *, minutes: int = 15) -> str:
    """A short-lived HS256 access token for `sub`."""
    if settings.jwt_secret is None:
        raise ValueError("JWT_SECRET is required to issue tokens")
    now = datetime.now(UTC)
    claims = {
        "sub": str(sub),
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "role": "authenticated",
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def dev_identity(username: str) -> UUID | None:
    """`parent`, or `parent:<label>` for a separate parent with a stable id."""
    if username == "parent":
        return PARENT
    role, _, label = username.partition(":")
    if role == "parent" and label.strip():
        return uuid5(NAMESPACE_URL, "habitquest-dev-parent:" + label.strip())
    return None


def dev_login(settings: Settings, username: str, password: str) -> UUID:
    """Check the shared development password (constant time) and resolve the identity; 401 otherwise."""
    expected = settings.dev_login_password
    identity = dev_identity(username)
    matches = expected is not None and hmac.compare_digest(
        password.encode(), expected.get_secret_value().encode()
    )
    if not matches or identity is None:
        raise HTTPException(401, "Incorrect username or password", headers={"WWW-Authenticate": "Bearer"})
    return identity


def bearer_token(settings: Settings) -> Callable[..., str | None]:
    """The request's bearer token. With DEV_LOGIN, /docs also offers the username/password login."""
    bearer = HTTPBearer(auto_error=False)

    def pasted(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> str | None:
        return credentials.credentials if credentials else None

    if not settings.dev_login:
        return pasted
    # Declared only so Swagger's Authorize dialog shows the password form; both read the same header.
    password = OAuth2PasswordBearer(tokenUrl=TOKEN_URL, auto_error=False)

    def either(token: Annotated[str | None, Depends(pasted)], _: Annotated[str | None, Depends(password)]):
        return token

    return either
