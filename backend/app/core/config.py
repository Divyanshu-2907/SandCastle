from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Sandcastle"
    environment: str = "development"

    # Required, with no fallback on purpose. A default would have to embed
    # credentials in source, and a wrong-but-working default fails silently by
    # connecting somewhere unintended. Copy .env.example to .env to set it.
    database_url: str
    redis_url: str = "redis://localhost:6379/0"

    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    jwt_secret: str = "dev-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24

    # Gemini is the default provider (free tier); Anthropic stays available.
    llm_provider: str = "gemini"
    agent_model: str = "gemini-2.5-flash"
    gemini_api_key: str = ""
    gemini_rpm: int = 10
    anthropic_api_key: str = ""
    agent_max_steps: int = 15

    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
