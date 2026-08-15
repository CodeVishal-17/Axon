"""FastAPI application factory.

``axon.main:app`` is the uvicorn entrypoint. The factory pattern keeps app
construction importable and testable (tests build their own instance), and
gives future tasks one obvious place to mount routers.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from axon import __version__
from axon.api.auth import router as auth_router
from axon.api.dashboard import router as dashboard_router
from axon.api.findings import router as findings_router
from axon.api.github import router as github_router
from axon.api.health import router as health_router
from axon.api.pull_requests import router as pull_requests_router
from axon.api.repos import router as repos_router
from axon.api.security import RateLimitMiddleware, SecurityHeadersMiddleware
from axon.api.webhooks import router as webhooks_router
from axon.config import get_settings
from axon.db.session import dispose_engine

logger = logging.getLogger("axon")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks. Deliberately does NOT touch the database on
    startup — the API must boot even if Postgres is briefly down; /healthz
    reports connectivity instead."""
    settings = get_settings()
    logger.info("starting %s (env=%s)", settings.app_name, settings.environment)
    yield
    dispose_engine()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()

    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # The interactive docs enumerate every route and schema — a free map of
    # the attack surface. They stay on in development (they feed `make types`
    # and are the demo-day console) and are withdrawn in production, where the
    # schema is generated from a checked-out tree, not scraped from the
    # running service. Override with EXPOSE_DOCS=true if you accept the risk.
    docs_enabled = settings.expose_docs or not settings.is_production
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )

    # Order matters: middleware added last runs first. Rate limiting should
    # reject a flood before anything else does work for it.
    app.add_middleware(SecurityHeadersMiddleware)
    # Compresses the findings/entities payloads, which are the large ones.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        # Explicit origins only. With allow_credentials the browser rejects
        # "*", but an accidental wildcard here would still hand the session
        # cookie to any site, so the value is validated in config.
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "X-Axon-Simulate-Secret"],
        max_age=600,
    )
    if settings.rate_limit_enabled:
        app.add_middleware(
            RateLimitMiddleware,
            default_limit=settings.rate_limit_default,
            sensitive_limit=settings.rate_limit_sensitive,
            window_s=settings.rate_limit_window_s,
        )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(repos_router)
    app.include_router(findings_router)
    app.include_router(dashboard_router)
    app.include_router(github_router)
    app.include_router(pull_requests_router)
    app.include_router(webhooks_router)
    # Future routers (graph, ask, fixes, ws) are mounted here as their
    # tasks land.

    return app


app = create_app()
