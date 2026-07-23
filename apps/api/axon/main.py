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

from axon import __version__
from axon.api.auth import router as auth_router
from axon.api.dashboard import router as dashboard_router
from axon.api.findings import router as findings_router
from axon.api.github import router as github_router
from axon.api.health import router as health_router
from axon.api.repos import router as repos_router
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

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # OpenAPI/docs stay enabled in all environments for now: the schema
        # feeds frontend type generation (T0.5) and /docs is the demo-day
        # debugging console.
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(repos_router)
    app.include_router(findings_router)
    app.include_router(dashboard_router)
    app.include_router(github_router)
    app.include_router(webhooks_router)
    # Future routers (graph, ask, fixes, ws) are mounted here as their
    # tasks land.

    return app


app = create_app()
