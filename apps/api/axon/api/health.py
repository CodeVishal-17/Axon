"""Liveness endpoint.

Semantics: /healthz answers 200 whenever the *process* is alive, and reports
database connectivity as data rather than failing the probe. Liveness and
dependency-health are different questions — an orchestrator restarting the
API in a loop because Postgres blipped would make an outage worse.
"""

import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from axon import __version__
from axon.config import get_settings
from axon.db.session import get_sessionmaker

logger = logging.getLogger("axon.health")

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Shape of the /healthz payload (also lands in the OpenAPI schema)."""

    status: str
    version: str
    environment: str
    database: str


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Report process liveness plus a best-effort database connectivity check."""
    settings = get_settings()

    database = "ok"
    try:
        with get_sessionmaker()() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — any DB failure means "unavailable"
        logger.warning("healthz database check failed: %s", exc)
        database = "unavailable"

    return HealthResponse(
        status="ok",
        version=__version__,
        environment=settings.environment,
        database=database,
    )
