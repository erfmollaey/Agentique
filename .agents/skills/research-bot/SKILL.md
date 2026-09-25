# Telegram AI Research Bot — Project Skill

**Purpose of this file:** brief an Agent that is about to write code in this repository. It is operational, not descriptive. Read it before touching code.

**Ground truth:** [`docs/ARCHITECTURE.md`](../../../docs/ARCHITECTURE.md) — the verified audit. If this skill and the audit disagree, the audit wins.

---

## Purpose

This project is a Telegram bot intended to become an AI research assistant: receive questions, search the web, read and compare sources, and produce answers with citations.

**It is currently a Phase 1 stabilization project, not a research product.** Your job in the active phase is to make the existing code reliable, secure, testable, and runnable — not to build research features.

The single most common failure in this repository is scope leakage: implementing a research capability because it seemed useful while doing infrastructure work. It is not useful. It is a scope violation.

---

## Mandatory Reading

Before modifying code, read in this order:

1. [`docs/PROJECT_PLAN.md`](../../../docs/PROJECT_PLAN.md) — active phase, status tables, dependency chain, decision log
2. [`docs/DEVELOPMENT_RULES.md`](../../../docs/DEVELOPMENT_RULES.md) — mandatory rules; violating them makes work incomplete
3. [`docs/phases/PHASE-01-INFRASTRUCTURE.md`](../../../docs/phases/PHASE-01-INFRASTRUCTURE.md) — the active phase
4. [`docs/ARCHITECTURE.md`](../../../docs/ARCHITECTURE.md) — verified current state and known defects with exact line numbers

Do not skip step 4. The audit records verified line numbers for every defect. Re-deriving them wastes effort and risks contradicting them.

---

## Current Architecture

### CURRENT

A two-process Python application. This is what exists.

```text
Telegram Bot API
      │  long polling (getUpdates) — no webhook
      ▼
┌───────────────────────────────────────────────────────────────┐
│ PROCESS 1 — FastAPI + aiogram          app/main.py           │
│                                                               │
│  startup event → asyncio.create_task(start_polling)          │
│  Dispatcher (app/bot/dispatcher.py)                           │
│    Bot(token=settings.BOT_TOKEN, parse_mode=HTML)             │
│  Handlers (app/bot/handlers.py)                               │
│    @dp.message(Command("start")) → start_command()            │
│    @dp.message()                  → research_handler()        │
│  GET /health → {"status":"ok"}   ← static literal            │
└──────────────────────────┬────────────────────────────────────┘
                           │ process_research.delay()
                           ▼
                   ┌───────────────┐
                   │ Redis broker  │  redis://localhost:6379/0
                   └───────┬───────┘
                           │
┌──────────────────────────▼────────────────────────────────────┐
│ PROCESS 2 — Celery worker     app/core/celery_app.py          │
│                                                               │
│  process_research(user_id, query)   app/tasks/research_task.py│
│    └─► Supervisor()  app/agents/supervisor.py                │
│         └─► analyze_query(query)                              │
│              POST api.groq.com/openai/v1/chat/completions  │
│              model="llama-3.3-70b-versatile", response_format=json │
│    └─► asyncio.new_event_loop() … bot.send_message()  ✗       │
└───────────────────────────────────────────────────────────────┘

  ┌──────────── NOT CONNECTED ─────────────┐
  │ PostgreSQL 16 in docker-compose         │  declared, unused
  │ DATABASE_URL in app/core/config.py      │  declared, unused
  │ asyncpg + sqlalchemy in requirements    │  declared, unused
  └─────────────────────────────────────────┘
```

Total: 8 Python files, ~150 statements. 12 Python files including 5 empty `__init__.py`.

---

## Technology Stack

### CURRENT — verified in the repository

| Layer | Technology | Version | Where |
|---|---|---|---|
| Language | Python | 3.12.0 (`.python-version`, `venv/lib/python3.12`) | — |
| Web framework | FastAPI | 0.115.6 | `app/main.py` |
| ASGI server | Uvicorn | 0.34.0 | runtime only, never imported |
| Telegram | aiogram | 3.17.0 | `app/bot/` |
| Telegram transport | Bot API long polling | — | `app/main.py:22` |
| Task queue | Celery | 5.4.0 | `app/core/celery_app.py` |
| Broker | Redis | 5.2.0 (client) | `REDIS_URL` in `.env` |
| LLM SDK | `openai` (OpenAI-compatible) | **missing from requirements.txt** | `app/agents/supervisor.py:3` |
| LLM endpoint | Groq-compatible | `https://api.groq.com/openai/v1` | `app/agents/supervisor.py:9` |
| LLM model | `llama-3.3-70b-versatile` | hardcoded | `app/agents/supervisor.py:14` |
| Settings | pydantic-settings | 2.7.0 | `app/core/config.py` |
| Containers | Docker Compose | `version: '3.8'` | `docker-compose.yml` |
| Broker service | `redis:7-alpine` | — | `docker-compose.yml:4-8` |
| Database service | `postgres:16-alpine` | — | `docker-compose.yml:10-20` |

### Declared but UNUSED

| Package | Status |
|---|---|
| `asyncpg` | Declared. Never imported. No database code exists. |
| `sqlalchemy` | Declared. Never imported. No database code exists. |
| `httpx` | Declared. Never imported. |
| `redis` (client) | Declared. Never imported. Broker is used via Celery. |
| `python-dotenv` | Declared. Never imported. Redundant with pydantic-settings. |

### Database — read this carefully

**PostgreSQL is NOT an active persistence layer.**

- A `postgres:16-alpine` service is declared in `docker-compose.yml:10-20`.
- `DATABASE_URL` is declared at `app/core/config.py:6`.
- `asyncpg` and `sqlalchemy` are pinned at `requirements.txt:6-7`.
- **Zero database code exists.** No models, no tables, no sessions, no migrations.

Do not write code that assumes persistence exists. Do not describe PostgreSQL as implemented. It is declared intent awaiting Phase 2.

### AI — read this carefully

The LLM client is constructed at module scope in `app/agents/supervisor.py:7-10` with a hardcoded key literal that does not match the configured endpoint. The credential is correct in neither sense: it is exposed in source, and it will not authenticate.

The intended `GROQ_API_KEY` is in `.env` and is **never read by any code**.

Provider abstraction is **PLANNED**, not current. There is exactly one hardcoded provider, one hardcoded model, and no indirection.

---

## Repository Structure

### CURRENT

```text
telegram-research-bot/
├── .env                        4 vars. Secrets. No .gitignore protects it.
├── .python-version             3.12.0
├── docker-compose.yml          redis + postgres ONLY
├── requirements.txt            10 pins; openai MISSING; 5 unused
├── docs/
│   ├── ARCHITECTURE.md         verified audit
│   ├── PROJECT_PLAN.md         master roadmap
│   ├── DEVELOPMENT_RULES.md    Agent governance
│   └── phases/                 PHASE-01 … PHASE-09
├── .agents/
│   └── skills/research-bot/
│       └── SKILL.md            this file
└── app/
    ├── __init__.py
    ├── main.py                 24 lines. FastAPI app, /health, polling.
    ├── agents/
    │   ├── __init__.py
    │   └── supervisor.py       65 lines. LLM client + Supervisor class.
    ├── bot/
    │   ├── __init__.py
    │   ├── dispatcher.py       10 lines. Bot + Dispatcher singletons.
    │   └── handlers.py         22 lines. /start + catch-all.
    ├── core/
    │   ├── __init__.py
    │   ├── celery_app.py       17 lines. Celery config.
    │   └── config.py           12 lines. Settings, 4 required vars.
    └── tasks/
        ├── __init__.py
        └── research_task.py    18 lines. The only Celery task.
```

### Absent — do not assume they exist

```text
Dockerfile          .gitignore        .env.example     .dockerignore
README.md           Makefile           pyproject.toml   CI config
alembic.ini         tests/             app/db/          app/services/
app/domain/         app/infrastructure/                 app/api/
```

---

## Runtime Architecture

### CURRENT — the actual message flow

```text
Telegram message
    ↓
aiogram Dispatcher                    app/bot/dispatcher.py:10
    ↓
research_handler()                    app/bot/handlers.py:15-22
    ├─ logging.info(f"...: {message.text}")   ← full user text logged
    ├─ message.answer("⏳ analyzing…")          ← always sent
    └─ process_research.delay(
           user_id=message.from_user.id,      ← used as chat_id later
           query=message.text)
    ↓
Redis broker                          app/core/celery_app.py
    ↓
Celery worker → process_research()    app/tasks/research_task.py:7
    ↓
Supervisor()                          instantiated per task
    ↓
analyze_query(query)                  app/agents/supervisor.py:16
    ├─ builds an f-string prompt — no system role
    ├─ client.chat.completions.create(
    │      model="llama-3.3-70b-versatile", temperature=0.3,
    │      response_format={"type":"json_object"})
    │      NO timeout, NO max_tokens, NO retry
    └─ json.loads(...)                ← unguarded
    ↓
"**summary** + sub-questions"        app/tasks/research_task.py:12
    ↓
asyncio.new_event_loop()              app/tasks/research_task.py:15
bot.send_message(chat_id=user_id)     app/tasks/research_task.py:17
loop.close()                          app/tasks/research_task.py:18
```

### CURRENT — known reliability problems

**This flow is not production-ready. It has four defects that break it.**

| Problem | Effect |
|---|---|
| New event loop per task, closed at the end, while the module-level `Bot`'s aiohttp session stays bound to the first loop | The core feature can succeed roughly **once per worker process**; sockets leak |
| `parse_mode=HTML` set globally, but output is `**markdown**`, unescaped, unsplit | Telegram rejects or mis-renders replies; replies are **silently lost** |
| Exactly one `try`/`except` in the whole codebase, guarding only polling startup | Every failure is **silent**; the user waits forever on "analyzing…" |
| No LLM timeout, no `max_tokens`, no Celery task time limit | A hung call holds a worker indefinitely; **cost is unbounded** |

Additional live defects: `message.from_user.id` is used as `chat_id`, so the bot is **broken in every group**; `openai` is missing from `requirements.txt`, so a **clean environment cannot start**; and a **hardcoded key literal** sits in source.

**The audit says the feature works once. Do not describe this flow as working.**

### PLANNED

```text
Telegram / Web  →  Application service  →  Domain logic  →  Infrastructure adapters
                            ↓
              PostgreSQL  ·  LLM provider  ·  Search provider  ·  Fetch (SSRF-guarded)
```

The service layer does not exist. Creating it is Phase 1 work.

---

## AI Architecture

### CURRENT

```text
app/agents/supervisor.py

client = OpenAI(api_key="<HARDCODED LITERAL>",      ← line 8, exposed in source
                base_url="https://api.groq.com/openai/v1")

class Supervisor:
    model = "llama-3.3-70b-versatile"                ← line 14, hardcoded

    analyze_query(user_query) -> dict               ← WORKS
        single f-string prompt, role="user" only
        no system prompt, no timeout, no max_tokens
        no retry, no fallback, no validation
        returns json.loads(content)                 ← unguarded

    generate_final_answer(user_query, sub_answers)   ← DEAD CODE
        zero callers, zero call edges
```

**This is not an agent system.** One class, one working method, one dead method, no tools, no orchestration, no state, no planning loop.

`analyze_query` does not answer questions. It decomposes one into a summary and 2–3 sub-questions — and `process_research` then discards them. The sub-questions are formatted into a message and dropped. Nothing is searched, nothing is fetched, nothing is cited.

### PLANNED

Research pipeline — none of this exists:

```text
Search → Fetch → Extract → Clean → Dedup → Rank → Analyze → Cite → Report
```

Multi-agent orchestration:

```text
Planner · Search · Reader · Source Evaluator · Fact Checker · Summarizer · Report Generator
```

Provider abstraction behind an interface, so the DeepSeek-compatible choice is reversible.

---

## Data Architecture

### CURRENT

**There is no data architecture.** Zero database code exists.

```text
Declared:  DATABASE_URL, asyncpg, sqlalchemy, postgres:16-alpine
Actual:    nothing
Missing:   models, tables, sessions, migrations, indexes, constraints
```

No user storage. No conversation storage. No message storage. No research storage. No source storage.

Every message is processed statelessly. `analyze_query` receives one string. The bot cannot hold a conversation, and nothing is recallable between sessions.

### PLANNED

```text
Phase 2:  User · Conversation · Message
Phase 3:  ResearchRun · Source · SourceChunk · Claim · Citation
Phase 4:  Document · plus chunk metadata
Phase 5:  Project · Session · Tag · Bookmark
```

**Design the Phase 2 schema so runs and documents can be associated with an optional project or collection from the start.** Retrofitting workspaces across every table after Phase 5 has landed is the most likely source of expensive rework in this project.

---

## Development Workflow

```text
1. Read the four mandatory documents
2. Confirm the active phase and that your task is listed in it
3. Read the actual implementation — do not trust names
4. Trace dependencies of the files you will change
5. Check the Architecture Decision Log for an existing decision
6. Plan: which files change, and why
7. Implement the smallest change that satisfies a stated requirement
8. Test — including the failure paths
9. Update documentation in the same change
10. Update the phase status and the decision log if you made a decision
```

**Step 3 is not optional.** The repository contains code whose names misrepresent its behavior: a function named `process_research` that performs no research, and a method named `generate_final_answer` that is never called. Verify by reading.

---

## Phase Rules

> **The active Phase is the Agent's scope boundary.**

1. Identify the active phase before writing code.
2. Implement only work belonging to the active phase.
3. Do not silently implement future-phase features. A useful idea is still out of scope.
4. If you discover a future-phase dependency, **document it** — do not build it.
5. Do not advance to the next phase until the current phase's acceptance criteria are satisfied.

### While Phase 1 is active

```text
ALLOWED
  reliability · security · infrastructure · configuration · tests
  architecture foundations · logging · health checks · error handling
  dependency correctness

NOT ALLOWED
  web search · PDF analysis · research workspace · multi-agent orchestration
  SaaS dashboard · monetization · any feature from Phases 2–9
```

**If a Phase 1 fix appears to require a research capability, stop and document the design question.** That is a signal to raise, not a reason to expand scope.

### Phase chain

```text
Phase 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9
```

Independent phases may proceed in parallel **only after their dependencies are satisfied by acceptance criteria**, not by starting. Research functionality must never become a hidden dependency inside Phase 1 or Phase 2.

---

## Security Rules

**Non-negotiable.**

- Never hardcode secrets. Not in code, comments, docstrings, or commented-out lines.
- Never print secrets — not in logs, errors, fixtures, docs, commit messages, or output.
- Never commit `.env`. Add `.gitignore` **before** the repository is initialized.
- Never expose API keys in logs.
- Treat user content, web content, and document content as untrusted.
- Consider prompt injection whenever external content enters a prompt.
- Never build a SQL string by concatenation.
- Never derive a path, URL, or command from user input.
- Do not weaken security to make a test pass.
- Report out-of-scope vulnerabilities with file, line, and severity. **Do not patch them silently.**

### Current security state

**SECRET FOUND IN FILE: `app/agents/supervisor.py`** — plaintext key literal at line 8, wrong format for the configured endpoint.

**SECRET FOUND IN FILE: `.env`** — bot token and provider API key.

**No `.gitignore` exists.** Not a git repository today, so nothing is committed. But one `git init && git add .` discloses both credentials and the entire `venv/`.

**Both credentials require manual rotation by a human.** This is Phase 1 requirement P0-3.

### Surfaces that do not exist yet

No SQL, no `subprocess`/`eval`/`exec`, no URL fetching, no unsafe deserialization, no file paths from user input. Those surfaces arrive in Phases 3 and 4, with SSRF and prompt injection as the defining risks there.

---

## Testing Rules

**Critical behavior must have regression tests.** A test is required for any change to the request path, the LLM call, message formatting, or the database layer.

**Test failure paths, not only success paths.** The most damaging defects in this repository are silent failures. A happy-path-only suite does not cover them.

**No external calls in unit tests.** LLM and Telegram clients must be substitutable. If a test needs the network, the design is wrong.

### The required Phase 1 regression test

> **Verify that `process_research` can execute successfully more than once within the same worker process.**

This exists because of the event-loop defect. It must be written so it **fails against the current implementation**. No test currently detects that defect.

**A phase is not complete without its tests.** See each phase document § Testing Requirements.

### Current state

Zero tests. No test files, no `tests/` directory, no test framework in `requirements.txt`, no CI.

---

## Documentation Rules

After implementing anything:

- Update the active phase document's Status section.
- Tick acceptance criteria **only when actually satisfied**.
- Update `docs/PROJECT_PLAN.md` — status tables and, if you made a decision, the decision log.
- Update `docs/ARCHITECTURE.md` when architecture changes. It is an audit snapshot; do not let it silently become false.
- Document what you did **not** do, and why.

### Honesty rules

Never write these without evidence at the moment of writing:

```text
"implemented"   "production ready"   "working"
"supported"     "completed"          "fully functional"
```

Use instead:

```text
Implemented     Partial      Skeleton    Planned
Not Implemented Blocked      In Progress Needs Verification
```

Insufficient evidence → write exactly:

```text
NOT VERIFIED — insufficient evidence in repository
```

### Never invent decisions

Do not name a vendor the repository has not named. Write intent, not a chosen provider. The open decisions are recorded as AD-006 (search provider), AD-007 (embeddings and vector store), AD-008 (hosting and cloud), and AD-009 (payment provider).

---

## Current Phase

**Phase 1 — Core Infrastructure & Stabilization**
**Status: Not Started**

The full specification is [`docs/phases/PHASE-01-INFRASTRUCTURE.md`](../../../docs/phases/PHASE-01-INFRASTRUCTURE.md). It contains 5 P0 items, 14 P1 items, the acceptance criteria, and a per-item status table.

### P0 — do these first

| ID | Task | Ref |
|---|---|---|
| P0-1 | Remove the hardcoded LLM key; read it from settings | C-1 |
| P0-2 | All credentials and endpoints from configuration | C-1 |
| P0-3 | **Rotate the exposed credentials — manual human action** | C-1, S-2 |
| P0-4 | Add `.gitignore` **before** the repo is initialized | S-2 |
| P0-5 | Add `.env.example`, placeholders only | S-2 |
| P0-6 | Add the missing `openai` dependency, pinned | C-2 |
| P0-7 | Fix the aiogram/asyncio event-loop lifecycle | C-3 |
| P0-8 | Resolve the parse-mode mismatch | C-4 |
| P0-9 | Handle Telegram message length limits | C-4 |
| P0-10 | Task and LLM error handling, always a user-facing outcome | H-2 |
| P0-11 | Explicit timeout on every LLM call | H-3 |
| P0-12 | Bounded retry with backoff | H-3 |
| P0-13 | Celery task time limits | H-3 |

### P1 — then these

| ID | Task | Ref |
|---|---|---|
| P1-1 | Correct `chat_id` handling | H-1 |
| P1-2 | Restrict the handler to text messages | H-7 |
| P1-3 | Real health and readiness checks | H-5 |
| P1-4 | Application lifecycle and graceful shutdown | H-6 |
| P1-5 | Introduce a service layer | M-4 |
| P1-6 | Dependency injection for `bot` and `client` | M-4 |
| P1-7 | Typed validation around LLM responses | H-4, M-10 |
| P1-8 | Rate limiting and cost control | M-8 |
| P1-9 | Celery configuration and result handling | M-9 |
| P1-10 | Dockerfile plus app and worker services | H-8 |
| P1-11 | Remove unused dependencies | M-5 |
| P1-12 | Consolidate logging, add correlation IDs | M-7 |
| P1-13 | Robust configuration loading | M-6 |
| P1-14 | Test harness and foundational tests | — |

**Suggested order:** P0-4, P0-5, P0-6, P0-1, P0-2 → P1-6, P1-5, P1-7 → **the regression test for P0-7** → P0-7 → P0-8 through P0-13 → P1-1, P1-2 → P1-3, P1-4 → P1-9, P1-12, P1-13 → P1-8 → P1-10, P1-11.

Injection and the service layer come before the tests, because both tests require them.

---

## Current Known Problems

Verified defects. Line numbers are from the audit and were confirmed against the source.

### CRITICAL

| ID | File | Problem |
|---|---|---|
| C-1 | `app/agents/supervisor.py:7-10` | Hardcoded key literal, wrong key format for the configured endpoint. Every LLM call fails; credential is exposed |
| C-2 | `requirements.txt` | `openai` imported at `supervisor.py:3` but not declared. A clean environment cannot start |
| C-3 | `app/tasks/research_task.py:15-18` | New event loop per task, closed, while the module-level `Bot`'s session stays bound to the first loop. Works roughly once per worker |
| C-4 | `app/bot/dispatcher.py:8` + `app/tasks/research_task.py:12,17` | `HTML` parse mode with unescaped, unsplit `**markdown**`. Telegram rejects or mis-renders; replies silently lost |

### HIGH

| ID | File | Problem |
|---|---|---|
| H-1 | `app/bot/handlers.py:20` | `from_user.id` used as `chat_id`; broken in every group |
| H-2 | `app/bot/handlers.py`, `app/tasks/research_task.py` | One `try`/`except` in the codebase. Every failure is silent |
| H-3 | `app/agents/supervisor.py:36,59`; `app/core/celery_app.py:11-17` | No timeout, no `max_tokens`, no retry, no task time limits |
| H-4 | `app/agents/supervisor.py:43`; `app/tasks/research_task.py:12` | Unguarded `json.loads` and dict access |
| H-5 | `app/main.py:11-13` | `/health` is a static literal; a dead bot reports healthy |
| H-6 | `app/main.py:15-18` | No graceful shutdown; deprecated `@app.on_event` |
| H-7 | `app/bot/handlers.py:15` | Catch-all with no content filter; non-text messages enqueue `query=None` |
| H-8 | `docker-compose.yml` | No application or worker service; no `Dockerfile` |

### MEDIUM and LOW

M-1 dead synthesis code · M-2 no database layer · M-3 no conversation memory · M-4 module-level singletons block testing · M-5 unused deps and imports · M-6 CWD-dependent config that crashes at import · M-7 duplicated logging, full user text logged · M-8 no rate limiting · M-9 unbounded result accumulation · M-10 untyped LLM contract · L-1 Python version drift · L-2 hardcoded English user-facing strings, no i18n · L-3 naming inconsistency · L-4 `chr(10)` in an f-string · L-5 hardcoded timezone · L-6 no project metadata files · L-7 unused `Dispatcher` in the worker · L-8 unguarded `from_user`

### Structural

- **No service layer.** `app/tasks/research_task.py` does composition, formatting, event-loop management, and transport in 18 lines.
- **Circular dependency.** `app/bot/handlers.py` imports from `app/tasks/`, and `app/tasks/research_task.py` imports `bot` from `app/bot/dispatcher.py`.
- **The graph detects two disconnected clusters** — the Telegram/FastAPI process and the Celery/agent process — joined only by a Redis queue. This is the root cause of most findings.

---

## Future Architecture

**Everything in this section is PLANNED. None of it exists.**

```text
Interfaces
  Telegram  ·  Web (Next.js)  ·  REST API  ·  Integrations
        ↓
Application
  services/  — chat · research · documents · workspace
        ↓
Domain
  pure logic, contracts, entity types — no I/O
        ↓
Infrastructure
  LLM provider · search provider · SSRF-guarded fetch · extractor
  PostgreSQL · Redis/Celery · file storage
```

Data model as it accumulates: `User · Conversation · Message` (P2) → `ResearchRun · Source · SourceChunk · Claim · Citation` (P3) → `Document` (P4) → `Project · Session · Tag · Bookmark` (P5) → accounts, organizations, memberships (P8) → `Plan · Subscription · UsageRecord · Invoice` (P9).

### Deliberately undecided — do not assume

| Decision | Log ref |
|---|---|
| Web search provider | AD-006 |
| Embedding provider and vector store | AD-007 |
| Hosting, cloud, deployment topology | AD-008 |
| Payment provider | AD-009 |
| Agent framework | Phase 6, undecided |
| PDF and charting libraries | Phase 7, undecided |
| File storage technology | Phase 4, undecided |

**Write intent, not a chosen vendor.** External services belong behind interfaces so the choice stays reversible.

---

## Completion Protocol

Before reporting work complete:

- [ ] Every file I changed is listed, with one line on why.
- [ ] Every requirement I satisfied maps to a numbered item in the active phase document.
- [ ] Tests were run, and I say so. **If they were not run, say that.** Never describe an unrun test as passing.
- [ ] Test results are reported as observed, not as expected.
- [ ] Phase acceptance criteria are ticked only where genuinely satisfied.
- [ ] Status updated in the phase document and in `docs/PROJECT_PLAN.md`.
- [ ] Any decision recorded in the Architecture Decision Log.
- [ ] Documentation updated — including what I did **not** do.
- [ ] Confirmed: no out-of-scope work. No Phase 2–9 functionality.
- [ ] Confirmed: no secret added, printed, logged, or committed.
- [ ] Confirmed: no dependency installed or upgraded without being asked.
- [ ] Confirmed: no migration run against any environment not designated for it.
- [ ] Confirmed: nothing committed unless explicitly asked.
- [ ] Anything I could not verify is reported as **NOT VERIFIED — insufficient evidence in repository**.

**If I am blocked, say so plainly.** Do not work around a blocker invisibly, and do not expand scope to route around it. Report the blocker, what I tried, and what decision is needed.
