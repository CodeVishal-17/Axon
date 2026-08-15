"""Transport-level hardening: response headers and request rate limiting.

Deliberately dependency-free. The rate limiter is an in-process fixed-window
counter, which is the honest shape of this deployment: one API container, one
process. It stops credential-stuffing and scripted abuse of the expensive
endpoints; it is NOT a distributed limiter, and if the API is ever scaled to
several replicas this must move to Redis (each replica would otherwise allow
the full quota).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from axon.config import get_settings

logger = logging.getLogger("axon.api.security")

# Header set kept small and uncontroversial. No CSP: this API serves JSON to a
# separate origin, and a wrong CSP here would be cargo cult.
_SECURITY_HEADERS = {
    # Never let a browser re-interpret a JSON body as HTML/JS.
    "X-Content-Type-Options": "nosniff",
    # The API has no UI worth framing.
    "X-Frame-Options": "DENY",
    # Don't leak repo ids in the Referer of outbound links.
    "Referrer-Policy": "no-referrer",
    # No reason for a JSON API to be reachable from a document's ambient
    # permissions.
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach hardening headers to every response."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in _SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        # HSTS only matters over TLS, and asserting it over plain http would
        # be a lie the browser caches.
        if get_settings().is_production and request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-client limiting, with a tighter budget for the paths
    that cost real money or protect credentials.

    Buckets are keyed by client IP + path class. The window is coarse on
    purpose: precision matters far less than refusing an obvious flood.
    """

    def __init__(self, app, *, default_limit: int, sensitive_limit: int, window_s: int):
        super().__init__(app)
        self.default_limit = default_limit
        self.sensitive_limit = sensitive_limit
        self.window_s = window_s
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)

    # Credential surface: brute-forcing sign-in must be expensive.
    _SENSITIVE_PREFIX = "/api/auth/"
    # Actions that spend money (an LLM call) or write to a customer's repo.
    # Read paths under /api/repos are deliberately NOT here — the feed polls
    # them, and throttling those would break the product, not an attacker.
    _SENSITIVE_MARKERS = ("/review", "/action", "/simulate-event")

    def _bucket(self, path: str) -> tuple[str, int]:
        if path.startswith(self._SENSITIVE_PREFIX) or any(
            marker in path for marker in self._SENSITIVE_MARKERS
        ):
            return "sensitive", self.sensitive_limit
        return "default", self.default_limit

    async def dispatch(self, request: Request, call_next):
        # Health checks must never be throttled — that is how the platform
        # decides whether the container is alive.
        if request.url.path == "/healthz":
            return await call_next(request)

        client = request.client.host if request.client else "unknown"
        bucket, limit = self._bucket(request.url.path)
        key = (client, bucket)
        now = time.monotonic()
        cutoff = now - self.window_s

        hits = self._hits[key]
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= limit:
            retry_after = max(1, int(self.window_s - (now - hits[0])))
            logger.warning(
                "rate limit hit: client=%s bucket=%s path=%s", client, bucket, request.url.path
            )
            return JSONResponse(
                {"detail": "Too many requests. Please slow down."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)

        # Opportunistic cleanup so idle clients don't accumulate forever.
        if len(self._hits) > 10_000:
            for stale_key in [k for k, v in self._hits.items() if not v]:
                self._hits.pop(stale_key, None)

        return await call_next(request)
