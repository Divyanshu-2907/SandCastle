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

    # Connection pool, per process. The defaults SQLAlchemy would pick (5 + 10
    # overflow) are sized for one big app server, not for N serverless
    # instances plus a worker all pointed at the same small Postgres: four
    # Cloud Run instances alone would reserve 60 of the ~25 connections a
    # db-f1-micro allows. Keep the product of (instances x (size + overflow))
    # under the server's max_connections.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Cloud SQL and most managed proxies drop idle connections; recycling below
    # that window turns a stale-socket error on the next query into a silent
    # reconnect.
    db_pool_recycle_seconds: int = 1800

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

    # Langfuse tracing. Off unless both keys are set -- there is no sensible
    # default for a credential, and a half-configured tracer should do nothing
    # rather than fail a run. Host is optional; the SDK falls back to Langfuse
    # Cloud. These are read but never logged or sent anywhere but the SDK.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    # Two names for one thing: LANGFUSE_HOST is what the SDK documents,
    # LANGFUSE_BASE_URL is what this project's .env already uses. Kept as two
    # fields rather than one aliased field because docker-compose passes both
    # through and an unset one arrives as "" -- an alias would treat that empty
    # string as a value, win over the other name, and silently send traces to
    # Langfuse Cloud instead of a configured self-hosted instance.
    langfuse_host: str = ""
    langfuse_base_url: str = ""

    @property
    def langfuse_endpoint(self) -> str:
        """The configured host, or "" to let the SDK pick its default."""
        return (self.langfuse_host.strip() or self.langfuse_base_url.strip())

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key.strip() and self.langfuse_secret_key.strip())

    # Mirrors SANDBOX_TIMEOUT, which agent/sandbox.py reads from the environment
    # directly (it runs without this Settings object loaded). Kept here so the
    # Celery time limits and the stale-run sweeper can be derived from the one
    # number that actually bounds a run.
    sandbox_timeout: int = 300

    # A run whose worker was SIGKILLed leaves its row in `running` with nobody
    # left to finish it, and a broker that lost a message leaves one in `queued`
    # forever -- Redis is not a durable queue. The sweeper fails both once they
    # are past these deadlines. Generous on purpose: marking a live run dead is
    # worse than reporting a dead one late.
    queued_stale_after_seconds: int = 1800
    # Clone + image pull + the agent loop, on top of the sandbox's own budget.
    running_stale_grace_seconds: int = 900
    stale_sweep_interval_seconds: int = 300

    @property
    def running_stale_after_seconds(self) -> int:
        return self.sandbox_timeout + self.running_stale_grace_seconds

    @property
    def task_soft_time_limit(self) -> int:
        """Celery-level backstop for a run the sandbox watchdog did not catch.

        The watchdog only bounds time spent *inside* the container; a run can
        also hang cloning, pulling an image, or waiting on an LLM socket.
        """
        return self.sandbox_timeout + 600

    @property
    def task_time_limit(self) -> int:
        # Hard kill, after the soft limit has had a chance to raise and let the
        # task record its own failure.
        return self.task_soft_time_limit + 120

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
