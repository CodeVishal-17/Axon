"""Job handlers, one module per job kind.

A handler is ``(db: Session, payload: dict) -> None``; raising marks the
job failed (with retry). The registry is resolved lazily so importing the
jobs package never drags in heavy service dependencies.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from axon.db.models import JobKind

Handler = Callable[[Session, dict[str, Any]], None]


class UnknownJobKind(RuntimeError):
    pass


def get_handler(kind: JobKind) -> Handler:
    from axon.jobs.handlers import generate_fix, ingest, verify

    registry: dict[JobKind, Handler] = {
        JobKind.INGEST: ingest.run,
        JobKind.VERIFY: verify.run,
        JobKind.GENERATE_FIX: generate_fix.run,
    }
    handler = registry.get(kind)
    if handler is None:
        raise UnknownJobKind(f"no handler registered for job kind {kind.value!r}")
    return handler
