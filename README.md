# Telegram AI Research Bot

A Telegram bot on FastAPI + aiogram with a Celery worker, currently a Phase 1
stabilization of a query-decomposition flow. Web research, documents, and
workspaces are **not implemented** — see the roadmap.

Full implementation status: [`docs/phases/PHASE-01-INFRASTRUCTURE.md`](docs/phases/PHASE-01-INFRASTRUCTURE.md)
Historical audit: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)

---

## Architecture

Two processes joined by a Redis queue.

```text
Telegram ──long polling──► api (FastAPI + aiogram)
                              │  enqueue
                              ▼
                           Redis
                              │
                              ▼
                           worker (Celery)
                              │
                              ├─► AI provider (OpenAI-compatible)
                              └─► Telegram sendMessage
```

| Layer | Location | Responsibility |
|---|---|---|
| Transport | `app/bot/` | Parse updates, delegate, respond. No business logic. |
| Application | `app/services/` | Orchestration, formatting, rate limiting, health. |
| Domain | `app/domain/` | Pure types and errors. No I/O. |
| Infrastructure | `app/infrastructure/` | Event loop, AI client, Telegram factory. |
| Tasks | `app/tasks/` | Celery entry points and worker lifecycle. |
| Config | `app/core/` | Settings, logging, Celery app. |

---

## Prerequisites

- Python 3.12+
- Redis (for the Celery broker)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An API key for an OpenAI-compatible provider

Docker and Docker Compose are optional; the stack runs either way.

---

## Setup

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env      # then fill in the values
```

`.env` is git-ignored and must never be committed.

### Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BOT_TOKEN` | yes | — | Telegram bot token |
| `GROQ_API_KEY` | yes | — | AI provider key |
| `LLM_BASE_URL` | no | `https://api.deepseek.com/v1` | Provider endpoint |
| `LLM_MODEL` | no | `deepseek-chat` | Model name |
| `LLM_TIMEOUT_SECONDS` | no | `60` | Per-request timeout |
| `LLM_MAX_TOKENS` | no | `1024` | Output token cap |
| `LLM_MAX_RETRIES` | no | `2` | Bounded transport retries |
| `REDIS_URL` | yes | — | Celery broker and result backend |
| `DATABASE_URL` | no | unset | Reserved for Phase 2; unused now |
| `RATE_LIMIT_MAX_REQUESTS` | no | `5` | Per user, per window |
| `RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Throttle window |
| `MAX_IN_FLIGHT_REQUESTS` | no | `8` | Global concurrency cap |
| `CELERY_TASK_TIME_LIMIT` | no | `120` | Hard task limit (s) |
| `CELERY_TASK_SOFT_TIME_LIMIT` | no | `100` | Soft task limit (s) |
| `CELERY_TASK_MAX_RETRIES` | no | `2` | Bounded retries |
| `TELEGRAM_PARSE_MODE` | no | `HTML` | `HTML`, `MARKDOWN_V2`, or `NONE` |
| `HEALTH_PROBE_TIMEOUT_SECONDS` | no | `2.0` | Readiness probe timeout |
| `LOG_LEVEL` | no | `INFO` | Root log level |
| `ENVIRONMENT` | no | `development` | Deployment mode |

`POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` are required only for
the Docker Compose `postgres` service.

The provider key must match the endpoint: a DeepSeek-format key will not
authenticate against an OpenRouter base URL, or the reverse.

---

## Running

Two processes. Both are required.

```bash
# terminal 1 — API + Telegram poller
uvicorn app.main:app --reload --port 8000

# terminal 2 — Celery worker
celery -A app.core.celery_app:celery_app worker --loglevel=INFO
```

The worker must be running, or messages are acknowledged and never answered.

### With Docker

```bash
# Requires POSTGRES_USER and POSTGRES_PASSWORD in .env
docker compose up --build
```

Compose provides `redis`, `postgres`, `api`, and `worker`. Ports bind to
`127.0.0.1` only.

---

## Endpoints

| Endpoint | Meaning |
|---|---|
| `GET /health` | Liveness. Cheap and local; checks no dependency. |
| `GET /ready` | Readiness. Probes the poller and the broker. 503 if either is down. |

`/health` returning `ok` does **not** mean Telegram or the broker are healthy.
Use `/ready` for that.

---

## Tests

```bash
pytest                    # full suite
pytest -q                 # quiet
pytest tests/test_t1_event_loop_regression.py   # the mandatory regression
ruff check app tests
```

Tests never touch the network: an autouse fixture fails any test that attempts
an outbound connection.

`tests/test_t1_event_loop_regression.py` covers audit C-3 — a task must be able
to run repeatedly in one worker process. It includes a canary that reproduces
the original per-task-loop failure, so the suite cannot silently stop
detecting the defect.

---

## Phase 1 limitations

Recorded deliberately, not oversights:

- **Rate limiting is in-process.** With more than one API process the counters
  stop being global. A shared store is required before scaling the API tier
  horizontally.
- **No database.** `DATABASE_URL`, `asyncpg`, and `sqlalchemy` are declared and
  unused. The data layer is Phase 2. `/ready` does not probe PostgreSQL, because
  claiming a check that does not exist would be false.
- **No retry across provider failover.** Retries are bounded and reuse the same
  model. A fallback model chain is Phase 2.
- **Long polling only.** No webhook, so no secret-token validation surface.
- **`Supervisor.generate_final_answer` is still unwired** (audit M-1). It is
  retained so no capability is removed by a stabilization phase, and is wired
  in Phase 3.
- **Credentials still require manual rotation.** See below.

---

## Security

- No secret is hardcoded in `app/`. A test enforces this.
- Secrets are `SecretStr`, so they cannot leak through `str()` or `repr()`.
- Log records are filtered to redact credential-shaped keys.
- Full user message text is never logged; only length and a short prefix.
- `.env` is git-ignored. `.env.example` contains placeholders only.

### Outstanding manual action

Two credentials were previously exposed and **must be rotated by a human**:

1. **Telegram bot token** — @BotFather → `/token` → revoke, then update `.env`.
2. **AI provider key** — revoke in the provider dashboard, then update `.env`.
3. **A previously hardcoded OpenRouter-format key** — revoke in the OpenRouter
   account. It sat in plaintext in source and may still be live.

Rotation requires provider dashboard access and cannot be automated.
