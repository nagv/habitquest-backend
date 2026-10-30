import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import NAMESPACE_URL, UUID, uuid5

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, OAuth2PasswordBearer

from habitquest_backend.config import Settings

# The fixed development parent; `parent:<label>` gives further parents with stable ids.
PARENT = UUID("11111111-1111-1111-1111-111111111111")
TOKEN_URL = "/v1/auth/token"
CAREGIVER, KID = "authenticated", "kid"
GOOGLE_CERTS = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
_google_keys: jwt.PyJWKClient | None = None


def verify_token(token: str, settings: Settings, *, role: str = CAREGIVER) -> UUID:
    """Verify one of our HS256 access tokens for `role` and return its subject: the user id for caregivers,
    the child id for kids. A kid token is refused where a caregiver is required, and the reverse.
    """
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
        if claims.get("role") != role:
            raise ValueError("Wrong kind of token")
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired token") from exc


def issue_token(settings: Settings, sub: UUID, *, minutes: int = 15, role: str = CAREGIVER) -> str:
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
        "role": role,
    }
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


def google_key(credential: str) -> Any:
    """Google's public signing key for this ID token (fetched from GOOGLE_CERTS and cached)."""
    global _google_keys
    if _google_keys is None:
        _google_keys = jwt.PyJWKClient(GOOGLE_CERTS, cache_keys=True, lifespan=3600)
    return _google_keys.get_signing_key_from_jwt(credential).key


def verify_google(credential: str, settings: Settings) -> dict:
    """Check a Google ID token from the frontend's sign-in button: Google's signature, our client id as the
    audience, a Google issuer, and a verified email. Returns its claims.
    """
    if not settings.google_client_id:
        raise HTTPException(503, "Google sign-in is not configured")
    try:
        claims = jwt.decode(
            credential,
            google_key(credential),
            algorithms=["RS256"],
            audience=settings.google_client_id,
            options={"require": ["exp", "iat", "iss", "sub", "aud"]},
        )
        if claims["iss"] not in GOOGLE_ISSUERS or claims.get("email_verified") is not True:
            raise ValueError("Unverified Google account")
    except jwt.PyJWKClientConnectionError as exc:
        raise HTTPException(503, "Google sign-in is unavailable; try again") from exc
    except (jwt.PyJWTError, ValueError) as exc:
        raise HTTPException(401, "Invalid Google sign-in") from exc
    return claims


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored:
        return False
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


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


def signed_in(settings: Settings, role: str = CAREGIVER) -> Callable[..., UUID]:
    """A dependency returning the caller's id for `role` (401 without a valid token of that kind)."""

    def caller(token: Annotated[str | None, Depends(bearer_token(settings))]) -> UUID:
        if token is None:
            raise HTTPException(401, "Authentication required")
        return verify_token(token, settings, role=role)

    return caller
