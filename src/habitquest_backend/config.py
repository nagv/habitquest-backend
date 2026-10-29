from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
    model_mode: Literal["fake", "ollama", "google"] = "fake"
    ollama_model: str = "qwen3:8b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    # Google Gemini through langchain-google-genai; set the model id your key can use.
    google_model: str = ""
    google_api_key: SecretStr | None = None
    # Run the planning worker loop on a thread inside the API process (single-service hosting).
    run_worker_in_api: bool = False
    jwt_issuer: str = "http://127.0.0.1:54321/auth/v1"
    jwt_audience: str = "authenticated"
    jwt_secret: SecretStr | None = None
    jwt_jwks_url: str = "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json"
    lease_seconds: int = Field(default=180, ge=10, le=900)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_model_calls: int = Field(default=12, ge=1, le=30)
    max_run_tokens: int = Field(default=128000, ge=1024, le=256000)
    max_run_seconds: int = Field(default=120, ge=1, le=600)

    @model_validator(mode="after")
    def google_configured(self) -> "Settings":
        if self.model_mode == "google" and not (self.google_model and self.google_api_key):
            raise ValueError("MODEL_MODE=google requires GOOGLE_MODEL and GOOGLE_API_KEY")
        return self
