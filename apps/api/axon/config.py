"""Application configuration.

Single source of truth for every environment-driven setting. All other
modules obtain configuration via :func:`get_settings` — nothing reads
``os.environ`` directly, so the full config surface is auditable here.

Settings load order (pydantic-settings): real environment variables win,
then values from a local ``.env`` file, then the defaults below.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All Axon backend settings, one field per environment variable."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Ignore unrelated env vars instead of failing on them.
        extra="ignore",
    )

    # --- Core ---
    app_name: str = "Axon API"
    environment: str = "development"  # development | production
    debug: bool = False
    log_level: str = "INFO"

    # --- Database ---
    # psycopg (v3) driver; sync SQLAlchemy — see axon/db/session.py for why.
    database_url: str = "postgresql+psycopg://axon:axon@localhost:5434/axon"

    # --- CORS ---
    # Kept as a plain comma-separated string (not list[str]) so it can be set
    # from a single env var on any hosting platform without JSON quoting.
    cors_origins: str = "http://localhost:3000"

    # --- Claim extraction ---
    # Entities per LLM-processing batch (also the embedding batch + commit
    # granularity in ClaimExtractionService).
    extraction_batch_size: int = 10

    # --- Entity linker ---
    linker_similarity_threshold: float = 0.60
    linker_top_k: int = 3
    linker_max_links_per_claim: int = 3

    # --- LLM provider ---
    llm_provider: str = "openai"  # openai | anthropic
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    # Sonnet per the project decision (DECISIONS.md); override via env.
    anthropic_model: str = "claude-sonnet-5"
    # Must produce EMBEDDING_DIM(=1536)-wide vectors (claims.embedding).
    # Embeddings always use OpenAI — Anthropic has no embeddings endpoint,
    # so OPENAI_API_KEY is required even when LLM_PROVIDER=anthropic.
    embedding_model: str = "text-embedding-3-small"

    # --- Job queue / worker ---
    worker_poll_interval_s: float = 2.0
    job_max_attempts: int = 3
    job_retry_backoff_s: float = 15.0
    # Must exceed the longest legitimate job duration: a running job holds
    # no row lock, so anything "running" older than this is a dead worker.
    job_stale_lock_seconds: float = 300.0

    # --- GitHub (consumed from T1.2 / T3.1 onward) ---
    github_token: str | None = None
    github_webhook_secret: str | None = None
    simulate_shared_secret: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins parsed into the list shape CORSMiddleware expects."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached so every caller shares one instance; tests can call
    ``get_settings.cache_clear()`` to re-read the environment.
    """
    return Settings()
