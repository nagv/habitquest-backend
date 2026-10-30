import hmac
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import NAMESPACE_URL, UUID, uuid5

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

from habitquest_backend.config import Settings

# Fixed development identities; a reviewer must differ from the author, a publisher from the builder.
PARENT = UUID("11111111-1111-1111-1111-111111111111")
EDITOR = UUID("e0000000-0000-0000-0000-0000000000e1")
REVIEWER = UUID("e0000000-0000-0000-0000-0000000000e2")
DEV_USERS = {"parent": (PARENT, False), "editor": (EDITOR, True), "reviewer": (REVIEWER, True)}
TOKEN_URL = "/v1/auth/token"


def verify_token(token: str, settings: Settings, *, hq_role: str | None = None) -> UUID:
    """Verify the JWT; with `hq_role`, also require the admin-assigned `app_metadata.hq_role` claim."""
    try:
        if settings.jwt_secret:
            key = settings.jwt_secret.get_secret_value()
            algorithms = ["HS256"]
        else:
            key = jwt.PyJWKClient(settings.jwt_jwks_url).get_signing_key_from_jwt(token).key
            algorithms = ["ES256", "RS256"]
        claims = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "sub", "iat"]},
        )
        if claims.get("role") != "authenticated":
            raise ValueError("Parent authentication required")
        user = UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired parent token") from exc
    if hq_role is not None and (claims.get("app_metadata") or {}).get("hq_role") != hq_role:
        raise HTTPException(403, f"{hq_role.capitalize()} role required")
    return user


def issue_token(settings: Settings, sub: UUID, *, editor: bool = False, minutes: int = 15) -> str:
    """A short-lived HS256 token; `editor` adds the admin-assigned `app_metadata.hq_role` claim."""
    if settings.jwt_secret is None:
        raise ValueError("JWT_SECRET is required for development tokens")
    now = datetime.now(UTC)
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


def dev_identity(username: str) -> tuple[UUID, bool] | None:
    """`parent`, `editor`, `reviewer`, or `parent:<label>` for a separate parent with a stable id."""
    if username in DEV_USERS:
        return DEV_USERS[username]
    role, _, label = username.partition(":")
    if role == "parent" and label.strip():
        return uuid5(NAMESPACE_URL, "habitquest-dev-parent:" + label.strip()), False
    return None


def dev_login(settings: Settings, username: str, password: str) -> tuple[UUID, bool]:
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
