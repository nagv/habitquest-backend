from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
    model_mode: Literal["fake", "ollama"] = "fake"
    ollama_model: str = "qwen3:8b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    jwt_issuer: str = "http://127.0.0.1:54321/auth/v1"
    jwt_audience: str = "authenticated"
    jwt_secret: SecretStr | None = None
    jwt_jwks_url: str = "http://127.0.0.1:54321/auth/v1/.well-known/jwks.json"
    lease_seconds: int = Field(default=180, ge=10, le=900)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_model_calls: int = Field(default=12, ge=1, le=30)
    max_run_tokens: int = Field(default=128000, ge=1024, le=256000)
    max_run_seconds: int = Field(default=120, ge=1, le=600)
