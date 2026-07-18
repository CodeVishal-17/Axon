"""Smoke test: the app builds and /healthz answers.

Deliberately passes with or without a reachable Postgres — /healthz reports
DB connectivity as data ("ok" / "unavailable"), it never fails liveness.
"""

from fastapi.testclient import TestClient

from axon.main import create_app


def test_healthz_returns_200_and_reports_components() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] in {"ok", "unavailable"}
    assert body["version"]
    assert body["environment"]
