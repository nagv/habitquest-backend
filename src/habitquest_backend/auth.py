from uuid import UUID

import jwt
from fastapi import HTTPException

from habitquest_backend.config import Settings


def verify_token(token: str, settings: Settings) -> UUID:
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
        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise HTTPException(401, "Invalid or expired parent token") from exc
