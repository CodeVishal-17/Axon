# Axon developer entrypoints.
# On Windows, run these from Git Bash (make ships with Git for Windows via
# `pacman`/scoop) or WSL; every target is also a one-liner you can paste.

COMPOSE := docker compose
API_DIR := apps/api

.PHONY: install db api dev down logs revision migrate test types eval-claims

## Install backend dependencies into the active virtualenv
install:
	cd $(API_DIR) && pip install -r requirements.txt -r requirements-dev.txt

## Start only Postgres (for running the API on the host with hot reload)
db:
	$(COMPOSE) up -d db

## Run the API locally against dockerized Postgres
api:
	cd $(API_DIR) && uvicorn axon.main:app --reload --port 8000

## Run the job worker locally against dockerized Postgres
worker:
	cd $(API_DIR) && python -m axon.jobs.worker

## Full stack in Docker (db + api)
dev:
	$(COMPOSE) up --build

## Stop everything (data volume is preserved)
down:
	$(COMPOSE) down

## Tail service logs
logs:
	$(COMPOSE) logs -f

## Create a new Alembic revision: make revision m="add claims table"
revision:
	cd $(API_DIR) && alembic revision --autogenerate -m "$(m)"

## Apply migrations to the configured database
migrate:
	cd $(API_DIR) && alembic upgrade head

## Run backend tests
test:
	cd $(API_DIR) && pytest -q

## Evaluate the claim-extraction prompt against the gold fixtures.
## `auto` runs the real LLM when API keys are configured, else falls back
## to the deterministic echo self-test. Pin a mode with EXTRACTOR=echo etc.
EXTRACTOR ?= auto
eval-claims:
	cd $(API_DIR) && python -m axon.evals.claims_eval --extractor $(EXTRACTOR)

## Evaluate the entity linker's deterministic tiers against gold fixtures
## (DB-free, offline). Prints the linking report + a random sample with
## per-link explanations.
eval-linker:
	cd $(API_DIR) && python -m axon.evals.linker_eval

## Evaluate the drift-verification prompt against seeded-drift and
## known-true fixtures. `auto` uses the real LLM when keys are configured,
## else the deterministic self-test. Pin with VERIFIER=scripted etc.
VERIFIER ?= auto
eval-verify:
	cd $(API_DIR) && python -m axon.evals.verify_eval --provider $(VERIFIER)

## Regenerate frontend API types from the backend's OpenAPI schema.
## Run after ANY change to backend routes or response models.
types:
	cd $(API_DIR) && python scripts/export_openapi.py ../web/openapi.json
	cd apps/web && npm run types
