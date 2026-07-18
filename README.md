# Axon

**A Truth Maintenance System for engineering organizations.**

Documentation, issues, and ADRs contain *beliefs*. Code, commits, and pull
requests represent *reality*. Axon continuously verifies beliefs against
reality and detects when knowledge becomes false.

Core loop: **Belief → Verify → Detect Drift → Act**

## Repository layout

```
apps/api   FastAPI backend (API server + background worker share this code)
apps/web   Next.js frontend (scaffolded in T0.3)
scripts    Demo seeding / reset utilities
```

## Quickstart (Docker, everything)

```bash
docker compose up --build
# API:     http://localhost:8000
# Health:  http://localhost:8000/healthz
# OpenAPI: http://localhost:8000/docs
```

## Quickstart (API on host, Postgres in Docker)

```bash
docker compose up -d db

cd apps/api
python -m venv .venv
source .venv/bin/activate        # Windows Git Bash: source .venv/Scripts/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env             # then fill in secrets as needed

uvicorn axon.main:app --reload --port 8000
```

## Common commands

See the [Makefile](Makefile) — `make dev`, `make db`, `make api`, `make test`,
`make revision m="..."`, `make migrate`.

## Configuration

All settings are environment variables loaded by `pydantic-settings`; see
[apps/api/.env.example](apps/api/.env.example) for the full list. Nothing is
read from anywhere else.
