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
| Infrastructure | `app/infrastructure/` | Event loop, AI client and provider factory, Telegram factory, search adapter and its factory (Phase 3). |
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
| `/research <question>` | Starts a research run: search the web, read sources, and
answer with citations. One acknowledgement, then one terminal message. The question
is bounded by `PLAN_MAX_QUESTION_CHARS` (2000); a question longer than that is
rejected with no run created. |

Any other `/command` is answered with a pointer to `/help` rather than treated as
chat text.

> `/research` is a Phase 3 capability implemented at P3.9-D; the
> `RESEARCH_MAX_SOURCES` allowance is described in the configuration table, and
> the deadline value is measurement-gated (see [`docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md) § U.4).

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
| `MAX_IN_FLIGHT_REQUESTS` | no | `8` | Global concurrency cap (unmeasured cross-process leak; B6-P4) |
| `RESEARCH_MAX_SOURCES` | no | `5` | Fetched and selected candidates per run (1..20) |
| `RESEARCH_DEADLINE_SECONDS` | no | `None` | Cooperative deadline per run (unbounded when unset); number is measurement-gated |
| `CELERY_TASK_TIME_LIMIT` | no | `120` | Hard task limit (s) |
| `CELERY_TASK_SOFT_TIME_LIMIT` | no | `100` | Soft task limit (s) |
| `CELERY_TASK_MAX_RETRIES` | no | `2` | Bounded retries |
| `TELEGRAM_PARSE_MODE` | no | `HTML` | `HTML`, `MARKDOWN_V2`, or `NONE` |
| `HEALTH_PROBE_TIMEOUT_SECONDS` | no | `2.0` | Readiness probe timeout |
| `LOG_LEVEL` | no | `INFO` | Root log level |
| `ENVIRONMENT` | no | `development` | Deployment mode |
| `SEARCH_PROVIDER` | no | `tavily` | Which search adapter `create_search_provider` builds (`brave` or `tavily`) |
| `SEARCH_API_KEY` | no | unset | Search provider API key. Optional because nothing searches yet; the factory refuses to build a provider without it |
| `SEARCH_MAX_RESULTS` | no | `5` | Results requested per query (max 20) |
| `SEARCH_TIMEOUT_SECONDS` | no | `15.0` | Client-side deadline for one provider call |
| `SEARCH_LANGUAGE` | no | unset | Language hint for queries that carry none |
| `SEARCH_REGION` | no | unset | Market hint for queries that carry none |
| `SEARCH_SAFE_SEARCH` | no | `strict` | `off`, `moderate`, or `strict` |

> **Web search is a Phase 3 capability.** The research command, orchestrator, and
> one-Celery-task-per-run execution now run end to end from a real command to a
> verified answer, and the adapter makes no unauthenticated network call in the
> repository. The number of candidates per run is bounded (`RESEARCH_MAX_SOURCES`)
> and every citation is verified against persisted evidence, but **a research run
> has not been measured end to end with credentials** — so the deadline and rate
> limit values are measurement-gated, and the provider was chosen on reachability
> and documented contracts, not on measured quality. See
> [`docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md`](docs/audits/PHASE-03_RESEARCH_ENGINE_AUDIT.md)
> § K, which also records the candidates considered and the trade-offs, and § U
> for the P3.9-D record.

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

Compose provides `redis`, `postgres`, `api`, `bot`, and `worker`. Ports bind to
`127.0.0.1` only.

`api` and `bot` are deliberately separate processes. `api` serves `/health` and
`/ready`; `bot` owns the Telegram long-polling loop. Running both roles in one
process works until a second process exists — Telegram delivers each update to
whichever consumer asks last, so two pollers do not give a redundancy, they give
messages dropped at random. Exactly one process polls, and `api` runs with
`TELEGRAM_POLLING=false` to guarantee it. `bot` exposes no port and is
healthchecked by a heartbeat it writes every 15s (`app/bot/heartbeat.py`); a
green status means the loop is turning, not that the bot has done any work.

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

**Verified since Phase 2 closed:** GitHub Actions execution (green) and the
Docker Compose configuration. The image was built and the API, worker, and
migration containers were run during Phase 2 with only the `FROM` line
substituted (Docker Hub was unreachable from that environment). **Still not
verified:** Telegram message delivery — `api.telegram.org` refuses the
connection from the development environment, so real `sendMessage` calls raised
`TelegramNetworkError`, which the application contained correctly; the delivery
logic is covered by substituted-transport tests. Everything else, including a
real end-to-end conversation against the live AI provider, was verified.

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
- **Credential rotation is complete.** The Phase 1 exposure was rotated before
  Phase 2; see "Credential exposure — resolved" below.

---

## Security

- No secret is hardcoded in `app/`. A test enforces this.
- Secrets are `SecretStr`, so they cannot leak through `str()` or `repr()`.
- Log records are filtered to redact credential-shaped keys.
- Full user message text is never logged; only length and a short prefix.
- `.env` is git-ignored. `.env.example` contains placeholders only.

### Credential exposure — resolved

Three credentials were exposed before this repository was initialised with git:
the Telegram bot token and the AI provider key in `.env`, plus a previously
hardcoded OpenRouter-format key that sat in plaintext in source. **All three
were rotated/revoked by a human before Phase 2** (Phase 1 items P0-3/SR-4).

A full scan of every commit confirms **no secret-shaped value exists anywhere
in this repository's git history**: `.env` was never committed, and the first
commit already contains the Phase 1 structure, so the pre-Phase-1 code that
held the hardcoded key never entered version control. The exposure predates git
and no committed secret remains to revoke. An earlier revision of this section
claimed the keys were recoverable from git history; that claim was checked
against the actual history and corrected here.
