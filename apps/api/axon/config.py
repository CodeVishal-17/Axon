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

    # --- Drift verification ---
    # Max claims verified per at-rest pass (economics knob, architecture §17)
    verify_budget: int = 50
    # Event-scoped passes are naturally small; this cap is a circuit
    # breaker for pathological mega-merges, keeping the live path fast.
    verify_event_budget: int = 25
    verify_max_source_chars: int = 8000

    # --- Remediation ---
    remediation_budget: int = 10
    remediation_min_confidence: float = 0.6

    # --- Pull-request review ---
    # Comments below this confidence are dropped (same discipline as
    # remediation); the char cap keeps a huge PR from blowing the context.
    review_min_confidence: float = 0.6
    review_max_diff_chars: int = 30_000
    review_max_files: int = 50
    review_max_claims: int = 40

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
    # PAT path (single-tenant / demo): authors PRs as the token owner.
    github_token: str | None = None
    github_webhook_secret: str | None = None
    simulate_shared_secret: str | None = None

    # --- GitHub App (multi-tenant: PRs authored by the app's bot identity) ---
    # When both an app id and a private key are set, the adapter authenticates
    # per installation instead of using github_token. Provide the key EITHER as
    # a file path (recommended: mount the .pem) OR inline (literal-\n tolerated).
    github_app_id: int | None = None
    github_app_private_key_path: str | None = None
    github_app_private_key: str | None = None

    # --- Auth (Sign in with GitHub) ---
    # The Axon GitHub App doubles as the OAuth provider: its Client ID +
    # Client Secret drive the user-authorization flow. session_secret signs the
    # session cookie (HMAC); web_base_url is where the callback redirects back.
    github_oauth_client_id: str | None = None
    github_oauth_client_secret: str | None = None
    session_secret: str | None = None
    session_ttl_hours: int = 720  # 30 days
    web_base_url: str = "http://localhost:3000"
    # Send the session cookie only over TLS. Defaults on in production; set
    # false ONLY for a plain-http deployment (a demo box on a bare IP), and
    # understand that the cookie is then sniffable in transit.
    session_cookie_secure: bool | None = None

    # --- Hardening ---
    # Interactive docs enumerate every route; withheld in production unless
    # explicitly re-enabled.
    expose_docs: bool = False
    rate_limit_enabled: bool = True
    rate_limit_window_s: int = 60
    rate_limit_default: int = 240      # generous: the UI polls
    rate_limit_sensitive: int = 30     # auth + LLM-spending endpoints

    @property
    def github_oauth_configured(self) -> bool:
        return bool(
            self.github_oauth_client_id
            and self.github_oauth_client_secret
            and self.session_secret
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """CORS origins parsed into the list shape CORSMiddleware expects.

        A wildcard is dropped: the API is served with credentials, and
        ``allow_origins=["*"]`` together with ``allow_credentials=True`` would
        hand the session cookie to any site that asks. Browsers reject that
        combination, but silently — so it is refused here where it is visible.
        """
        origins = [
            origin.strip()
            for origin in self.cors_origins.split(",")
            if origin.strip()
        ]
        safe = [origin for origin in origins if origin != "*"]
        if len(safe) != len(origins):
            import logging  # noqa: PLC0415

            logging.getLogger("axon.config").error(
                "CORS_ORIGINS contains '*', which cannot be combined with "
                "credentialed requests — ignoring it. List real origins."
            )
        return safe

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cookie_secure(self) -> bool:
        """Whether the session cookie carries the Secure flag."""
        if self.session_cookie_secure is not None:
            return self.session_cookie_secure
        return self.is_production


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached so every caller shares one instance; tests can call
    ``get_settings.cache_clear()`` to re-read the environment.
    """
    return Settings()
