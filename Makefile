# Phase 1 + Phase 2 developer commands.
#
#   make install   set up the environment
#   make test      run the test suite (needs PostgreSQL; see migrate-test-db)
#   make lint      static checks
#   make migrate   apply database migrations
#   make run-api   start the API/poller
#   make run-worker start the Celery worker
#   make up        start the full Docker stack
#   make verify    everything CI would run

PYTHON ?= venv/bin/python
PIP    ?= venv/bin/pip

.PHONY: install install-dev test test-verbose test-regression lint format typecheck \
        verify migrate migrate-downgrade revision check-migrations \
        run-api run-worker up down logs clean

install:
	python3.12 -m venv venv
	$(PIP) install -r requirements.txt

install-dev:
	python3.12 -m venv venv
	$(PIP) install -r requirements.txt -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q

test-verbose:
	$(PYTHON) -m pytest -v

# The mandatory Phase 1 regression test on its own.
test-regression:
	$(PYTHON) -m pytest tests/test_t1_event_loop_regression.py -v

lint:
	$(PYTHON) -m ruff check app tests

format:
	$(PYTHON) -m ruff format app tests

typecheck:
	$(PYTHON) -m mypy app

verify: lint typecheck test

# --- Database (Phase 2) ---------------------------------------------------
#
# The URL comes from DATABASE_URL in .env. Override with ALEMBIC_DATABASE_URL,
# which takes precedence and is the supported way to target another database
# without editing a tracked file.

migrate:
	$(PYTHON) -m alembic upgrade head

migrate-downgrade:
	$(PYTHON) -m alembic downgrade base

# Autogenerate a new revision. Review the generated file: it is a starting
# point, not a finished migration.
revision:
	$(PYTHON) -m alembic revision --autogenerate -m "$(m)"

# Fail if the models and the migrations have drifted apart. Run this in CI so a
# model edit that was never migrated cannot reach a deployment.
check-migrations:
	$(PYTHON) -m alembic check

# --- Runtime --------------------------------------------------------------

run-api:
	$(PYTHON) -m uvicorn app.main:app --reload --port 8000

run-worker:
	$(PYTHON) -m celery -A app.core.celery_app:celery_app worker --loglevel=INFO

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f api worker

clean:
	find . -type d -name __pycache__ -not -path './venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
