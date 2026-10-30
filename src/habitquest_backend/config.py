from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://hq_dev@127.0.0.1:55432/habitquest"
    # Our own HS256 access tokens; their `sub` is hq.users.id.
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "http://127.0.0.1:8000"
    jwt_audience: str = "authenticated"
    # Development login: POST /v1/auth/token (and /docs Authorize) issues tokens for test parents.
    dev_login: bool = False
    dev_login_password: SecretStr | None = None

    @model_validator(mode="after")
    def modes_configured(self) -> "Settings":
        if self.dev_login:
            password = self.dev_login_password.get_secret_value() if self.dev_login_password else ""
            if len(password) < 12 or not self.jwt_secret:
                raise ValueError("DEV_LOGIN requires JWT_SECRET and a DEV_LOGIN_PASSWORD of 12+ characters")
        return self
