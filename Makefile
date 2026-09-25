# Phase 1 developer commands.
#
#   make install   set up the environment
#   make test      run the test suite
#   make lint      static checks
#   make run-api   start the API/poller
#   make run-worker start the Celery worker
#   make up        start the full Docker stack
#   make verify    everything CI would run

PYTHON ?= venv/bin/python
PIP    ?= venv/bin/pip

.PHONY: install install-dev test lint format typecheck verify \
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

verify: lint test

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
