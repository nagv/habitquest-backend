from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://hq_dev@127.0.0.1:55432/habitquest"
    # Our own HS256 access tokens; their `sub` is hq.users.id (or the child id for kid tokens).
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "http://127.0.0.1:8000"
    jwt_audience: str = "authenticated"
    # Google sign-in: the OAuth web client id the frontend's sign-in button uses.
    google_client_id: str = ""
    # Browser origins allowed to call the API, comma-separated (the frontend's dev server by default).
    cors_origins: str = "http://localhost:5180"
    # Mapping pain points to goals: `fake` is a deterministic keyword-based stand-in (tests, local use);
    # `openai` calls any OpenAI-compatible chat endpoint, e.g. Nebius.
    ai_mode: Literal["fake", "openai"] = "fake"
    openai_base_url: str = ""
    openai_model: str = ""
    openai_api_key: SecretStr | None = None
    ai_timeout_seconds: float = Field(default=15, gt=0, le=60)
    # Development login: POST /v1/auth/token (and /docs Authorize) issues tokens for test parents.
    dev_login: bool = False
    dev_login_password: SecretStr | None = None

    @model_validator(mode="after")
    def modes_configured(self) -> "Settings":
        if self.ai_mode == "openai" and not (
            self.openai_base_url and self.openai_model and self.openai_api_key
        ):
            raise ValueError("AI_MODE=openai requires OPENAI_BASE_URL, OPENAI_MODEL and OPENAI_API_KEY")
        if self.dev_login:
            password = self.dev_login_password.get_secret_value() if self.dev_login_password else ""
            if len(password) < 12 or not self.jwt_secret:
                raise ValueError("DEV_LOGIN requires JWT_SECRET and a DEV_LOGIN_PASSWORD of 12+ characters")
        return self

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
