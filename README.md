# Telegram AI Research Bot

A Telegram bot on FastAPI + aiogram with a Celery worker and PostgreSQL
persistence. It holds a **persistent, multi-turn AI conversation**: messages,
history, and replies survive a restart, and the model sees prior turns.

It performs **no web research**. Search, sources, citations, and documents are
later phases — see the roadmap.

Full implementation status: [`docs/phases/PHASE-02-AI-CHAT.md`](docs/phases/PHASE-02-AI-CHAT.md)
Phase 1 record: [`docs/phases/PHASE-01-INFRASTRUCTURE.md`](docs/phases/PHASE-01-INFRASTRUCTURE.md)
Historical audit: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — describes the repository *before* Phase 1; it is a baseline, not current state.

---

## Architecture

Two processes joined by a Redis queue, over PostgreSQL.

```text
Telegram ──long polling──► api (FastAPI + aiogram)
                              │  acknowledge + enqueue
                              ▼
                           Redis
                              │
                              ▼
                           worker (Celery)
                              │
                              ▼
                     ChatService ──► repositories ──► PostgreSQL
                              │
                              ├─► context builder (order, budget, truncate)
                              ├─► AI provider (OpenAI-compatible, validated)
                              └─► Telegram sendMessage
```

A text message is acknowledged immediately and answered asynchronously. Commands
(`/help`, `/new`, `/conversations`, `/reset`) are answered inline, because they
touch the database but never the provider.

| Layer | Location | Responsibility |
|---|---|---|
| Transport | `app/bot/` | Parse updates, delegate, respond. No business logic, no SQL. |
| Application | `app/services/` | Chat orchestration, context assembly, system prompt, formatting, delivery, rate limiting, health. |
| Domain | `app/domain/` | Pure types, errors, and the ports the application depends on. No I/O. |
| Persistence | `app/db/`, `app/repositories/` | The only place SQLAlchemy is imported. |
| Infrastructure | `app/infrastructure/` | Event loop, AI client and provider factory, Telegram factory. |
| Tasks | `app/tasks/` | Celery entry points and worker lifecycle. |
| Config | `app/core/` | Settings, logging, Celery app. |

### Data model

```text
users ──< conversations ──< messages
  │              │
  │              └── role: user | assistant | system
  │                  turn_id pairs a question with its reply
  └── telegram_user_id UNIQUE
```

A user may hold many conversations; the active one is the most recently updated
`active` conversation. Messages are ordered by `(created_at, id)`.

### Commands

| Command | Effect |
|---|---|
| `/start` | Introduction. |
| `/help` | Lists the commands above. |
| `/new` | Archives the current conversation and starts a new one. |
| `/conversations` | Lists your conversations, newest first. |
| `/reset` | Asks for confirmation. |
| `/reset confirm` | Deletes the current conversation's messages. The conversation and your user record both survive. |
| `/delete_account` | Asks for confirmation. Nothing is deleted yet. |
| `/delete_account confirm` | **Irreversible.** Deletes your account and everything stored about you: every conversation and every message in them. |
| `/delete_account cancel` | Cancels. Nothing is deleted. |

Any other `/command` is answered with a pointer to `/help` rather than treated as
chat text.

---

## Prerequisites

- Python 3.12+
- Redis (for the Celery broker)
- **PostgreSQL** (chat persistence) — required as of Phase 2
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
| `LLM_MODEL` | no | `qwen/qwen3.8-27b` | Model name |
| `LLM_PROVIDER` | no | `openai_compatible` | Provider implementation selector |
| `LLM_TIMEOUT_SECONDS` | no | `60` | Per-request timeout |
| `LLM_MAX_TOKENS` | no | `1024` | Output token cap |
| `LLM_MAX_RETRIES` | no | `2` | Bounded transport retries |
| `LLM_FALLBACK_MODELS` | no | unset | Comma-separated fallback chain |
| `LLM_FALLBACK_MAX_ATTEMPTS` | no | `1` | Bound on the fallback chain |
| `LLM_CONTEXT_MAX_MESSAGES` | no | `20` | Most recent messages sent to the model |
| `LLM_CONTEXT_MAX_TOKENS` | no | `4000` | Context token budget |
| `LLM_CHARS_PER_TOKEN` | no | `4.0` | Token-estimate divisor |
| `CHAT_MAX_MESSAGE_CHARS` | no | `4000` | Inbound message cap |
| `DATABASE_URL` | yes | — | PostgreSQL connection URL |
| `DATABASE_POOL_SIZE` | no | `5` | Connection pool size |
| `DATABASE_POOL_MAX_OVERFLOW` | no | `5` | Pool overflow |
| `DATABASE_ECHO` | no | `false` | Log SQL |
| `TEST_DATABASE_URL` | no | unset | Test database; name must end in `_test` |
| `REDIS_URL` | yes | — | Celery broker and result backend |
| `RATE_LIMIT_MAX_REQUESTS` | no | `5` | Per user, per window |
| `RATE_LIMIT_WINDOW_SECONDS` | no | `60` | Throttle window |
| `RATE_LIMIT_DAILY_MAX_REQUESTS` | no | `200` | Per-user daily allowance |
| `RATE_LIMIT_DAILY_WINDOW_SECONDS` | no | `86400` | Daily allowance window |
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

The provider key must match the endpoint: a Groq-format key will not
authenticate against an OpenRouter base URL, or the reverse. The shipped default
is Groq; any OpenAI-compatible endpoint works by changing `LLM_BASE_URL` and
`LLM_MODEL`.

Check that the endpoint and model you configured are actually served, before
assuming a credential problem:

```bash
curl -s -o /dev/null -w '%{http_code}\n' \
  -H "Authorization: Bearer $GROQ_API_KEY" \
  https://api.groq.com/openai/v1/models          # 200 = key is valid
```

`401` means the key is wrong. `404 model_not_found` on a completion means the key
is fine and the **model** has been retired — which is what happened to the
originally configured `llama-3.3-70b-versatile`.

### Migrations

Apply the schema before starting. Never edit tables by hand.

```bash
createdb research_db                     # once
make migrate                             # alembic upgrade head
make check-migrations                    # fail if models and migrations drifted
```

`make migrate` reads `DATABASE_URL` from `.env`. To target another database
without editing a tracked file:

```bash
ALEMBIC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@host/dbname make migrate
```

The test database is separate and its name **must** end in `_test`:

```bash
createdb research_db_test
export TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/research_db_test
```

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
| `GET /ready` | Readiness. Probes the poller, the broker, and the database. 503 if any is down. |

`/health` returning `ok` does **not** mean Telegram, the broker, or the database
are healthy. Use `/ready` for that.

---

## Tests

```bash
make verify        # ruff + mypy + pytest — the same gate CI runs
pytest -q
pytest tests/test_t1_event_loop_regression.py   # the mandatory regression
```

**The suite needs a real PostgreSQL.** Migration, constraint, cascade, and
index behaviour is specified against a real database, so SQLite is not used.

```bash
createdb research_db_test
export TEST_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@localhost:5432/research_db_test
```

Isolation is enforced, not merely intended: the suite refuses any database whose
name does not end in `_test`, so a misconfigured `TEST_DATABASE_URL` fails loudly
instead of truncating real data. Schema tests build the schema by running the
real migrations, and `alembic check` fails if the models have drifted from them.

Tests never touch the network or a live provider: an autouse fixture fails any
outbound connection other than loopback, and the AI provider and Telegram
transport are both substituted.

`tests/test_t1_event_loop_regression.py` covers audit C-3 — a task must be able
to run repeatedly in one worker process. It includes a canary that reproduces
the original per-task-loop failure, so the suite cannot silently stop
detecting the defect.

---

## Known limitations

Recorded deliberately, not oversights. The full list, with the reasoning behind
each trade-off, is in
[`docs/phases/PHASE-02-AI-CHAT.md`](docs/phases/PHASE-02-AI-CHAT.md) § 14.

**Not verified in the environment Phase 2 was completed in:** Telegram message
delivery (the Telegram API refuses the connection), GitHub Actions execution,
and `docker build` against the exact pinned base image (Docker Hub is blocked
here; a build differing only in the `FROM` line succeeded). Everything else,
including a real end-to-end conversation against the live AI provider, was
verified.

- **Rate limiting and the daily allowance are in-process.** With more than one
  API process the counters stop being global. A shared store is required before
  scaling the API tier horizontally (`AD-012`, `AD-025`).
- **Deletion is per-account, not per-conversation.** `/reset` clears one
  conversation and keeps you. `/delete_account confirm` removes your user record
  and everything cascading from it, in one transaction. There is no export, no
  grace period, and no undo — a deliberate MVP scope choice, not an oversight.
- **Context is truncated, never summarized.** A long conversation loses its
  oldest turns beyond the configured window. The model is told this happened
  rather than left to infer it. No summarization, no embeddings (`AD-022`).
- **Token counting is an estimate.** A deterministic character-ratio heuristic
  behind an interface a real tokenizer can replace. *Recorded* usage comes from
  the provider, so cost accounting is accurate regardless.
- **The daily allowance counts requests, not tokens.**
- **A turn is two database transactions**, so a stored question with no reply is
  a legitimate intermediate state. The retry path detects and completes it
  (`AD-018`).
- **This is not a distributed transaction.** A Telegram send that fails after a
  successful commit leaves the reply stored but undelivered; a redelivery
  re-sends the stored reply rather than generating a new one.
- **The provider call is synchronous inside an async service.** Acceptable at
  `--concurrency=1`; must be revisited before the worker scales concurrency
  within a process.
- **Long polling only.** No webhook, so no secret-token validation surface.
- **`conversations.project_id` is a reserved, unused column** for the Phase 5
  project association (`AD-015`).
- **`Supervisor.analyze_query` is retained but unwired** (audit M-1). Phase 3
  needs the decomposition capability, so this phase kept it rather than
  replacing it.
- **The Celery task is still named `process_research`.** Inherited from Phase 1
  under the no-rename rule (`AD-027`).
- **Credentials still require manual rotation.** See below.

---

## Security

- No secret is hardcoded in `app/`. A test enforces this.
- Secrets are `SecretStr`, so they cannot leak through `str()` or `repr()`.
- Log records are filtered to redact credential-shaped keys.
- Full user message text is never logged; only length and a short prefix.
- `.env` is git-ignored. `.env.example` contains placeholders only.

### Outstanding manual action

> **This is now a live exposure, not a theoretical one.** The provider key in
> `.env` was confirmed working against the live API during Phase 2 completion
> (HTTP 200 on `/models`). It sits in this repository's git history, where a
> previously hardcoded OpenRouter-format key also remains. Rotate both.

Two credentials were previously exposed and **must be rotated by a human**:

1. **Telegram bot token** — @BotFather → `/token` → revoke, then update `.env`.
2. **AI provider key** — revoke in the provider dashboard, then update `.env`.
3. **A previously hardcoded OpenRouter-format key** — revoke in the OpenRouter
   account. It sat in plaintext in source and may still be live.

Rotation requires provider dashboard access and cannot be automated.
