# Architecture & Technical Audit — Telegram AI Research Bot

> ## ⚠️ HISTORICAL AUDIT SNAPSHOT — PRESERVED UNMODIFIED
>
> **This document describes the repository as it was on 2026-09-25, BEFORE Phase 1
> implementation. It is not a statement of the current state.**
>
> Phase 1 — Core Infrastructure & Stabilization has since resolved the findings
> recorded here: C-1, C-2, C-3, C-4, H-1 through H-7, M-5 through M-10, and S-2.
> Two remain partial (H-8 containerisation, and the CI tail of SR-10) and one is
> blocked on a manual human action (credential rotation, P0-3 / SR-4).
>
> Everything below is retained **verbatim as the defect baseline**. Its line
> numbers, findings, and severity judgements describe the pre-Phase-1 code and
> must not be "corrected" to match the new implementation — that would destroy
> the record of what was wrong and why.
>
> **For the current state, see:**
> - [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) — maturity table and decision log
> - [`phases/PHASE-01-INFRASTRUCTURE.md`](./phases/PHASE-01-INFRASTRUCTURE.md) § 14 — implementation record, validation evidence, and open blockers
> - [`../README.md`](../README.md) — how to run the system
>
> Seven additional defects were found *during* Phase 1 implementation and are
> recorded in the Phase 1 document § 14, not here, because they did not exist
> when this audit was taken.
>
> ### Update — 2026-09-26, after Phase 2 (AI Chat MVP)
>
> Phase 2 has since been implemented. The audit findings below are **still not
> edited**; they remain the pre-Phase-1 baseline. The structural conclusions that
> Phase 2 changed are noted here so a reader is not misled by § 7's "no service
> layer" or § 6's "no database code" when looking at the current code:
>
> - **A service layer now exists** (`app/services/`), with a chat service owning
>   conversation orchestration and a context service owning ordering, budgeting,
>   and truncation. `app/services/research.py` was removed and its delivery-neutral
>   contract moved to `app/services/chat.py` (`AD-026`).
> - **A data layer now exists** (`app/db/`, `app/repositories/`): `users`,
>   `conversations`, `messages`, with Alembic migrations. Audit finding M-2
>   ("no database layer") and M-3 ("no conversation memory") are resolved.
> - **`DATABASE_URL` is now required** and wired into a real async engine. The
>   `/ready` endpoint, which this audit's H-5 finding correctly said did not
>   probe PostgreSQL, now does.
> - **The request path is no longer a query decomposition.** It is a persistent
>   multi-turn chat. `Supervisor.analyze_query` (M-1's dead-code finding) is
>   retained but unwired, for Phase 3.
> - **The single hardcoded provider is now selected by configuration**, behind a
>   factory, with a bounded fallback chain. The endpoint is Groq rather than the
>   DeepSeek-compatible one named in this audit's § 5.
> - **The `bot ↔ tasks` circular dependency** this audit recorded as structural
>   was already broken in Phase 1 and remains broken.
>
> Six further defects were found during Phase 2 and are recorded in
> [`phases/PHASE-02-AI-CHAT.md`](./phases/PHASE-02-AI-CHAT.md) § 14.
>
> ### Update — 2026-09-26, Phase 2 completion pass
>
> Two changes since the note above, both recorded rather than folded into the
> audit text:
>
> - **Account deletion exists (SR-9).** `/delete_account confirm` removes the
>   user row; the foreign keys cascade to conversations and messages. One
>   statement, one transaction. The cascade was verified against real PostgreSQL
>   rather than inferred from the schema, and proven narrow by deleting one of
>   two users and checking the survivor. This is the only capability the audit's
>   "no user accounts" line could be read as contradicting, so it is called out.
> - **The LLM endpoint is Groq, not the DeepSeek-compatible one** named in § 5 of
>   this audit, and the model is `qwen/qwen3.8-27b`. The client class is
>   unchanged, so the "one hardcoded provider" finding is still resolved — the
>   difference is configuration, recorded as `AD-028`.
>
> Two defects found in this pass are recorded in the Phase 2 document, not here:
> every secret scanner in the repository was blind to Groq's `gsk_` key format
> (`P2-7`), and the configured model had been retired by the provider (`P2-8`).

**Audit type:** READ-ONLY technical audit
**Repository:** `/Users/erfmollaey/projects/telegram-research-bot`
**Date:** 2026-09-25
**Scope:** Full repository, excluding `venv/` and `__pycache__/`

**Method:** Every non-`venv` file was read directly (8 Python files, `requirements.txt`, `docker-compose.yml`, `.env`, `.python-version`). Findings were cross-checked against the codebase knowledge graph (call edges, dependency edges, route table, coverage report). No files were modified, no packages installed, no migrations run, no configuration changed, no commits made.

**Coverage note:** Index coverage for this repository reported `parse_partial: 0` and `skipped: 0`. All source was additionally read directly, which is stronger evidence than the graph. Findings below cite exact file paths and line numbers.

**Secret handling:** No secret values are reproduced in this document. Secrets are referenced by file path only.

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Architecture](#2-current-architecture)
3. [Implemented Features](#3-implemented-features)
4. [Current Message Flow](#4-current-message-flow)
5. [Security Findings](#5-security-findings)
6. [Bugs & Technical Issues](#6-bugs--technical-issues)
7. [Code Quality Findings](#7-code-quality-findings)
8. [Testing Gaps](#8-testing-gaps)
9. [Roadmap Status](#9-roadmap-status)
10. [Recommended Next Steps](#10-recommended-next-steps)
11. [P0/P1/P2/P3 Priority List](#11-p0p1p2p3-priority-list)
12. [Overall Project Readiness](#12-overall-project-readiness)
13. [What To Build Next](#13-what-to-build-next)
14. [Appendix A — Repository Inventory](#appendix-a--repository-inventory)
15. [Appendix B — Verified Call Graph](#appendix-b--verified-call-graph)

---

## 1. Executive Summary

This project is a **skeleton, not a working research bot**. The entire codebase is 8 Python files totaling roughly 150 statements. What exists is a thin vertical slice: FastAPI process → aiogram long polling → one catch-all handler → Celery `.delay()` → one DeepSeek call that splits a question into sub-questions → text sent back to Telegram.

Of the 9 stated product goals, **exactly one is partially implemented** (receive questions via Telegram and get an LLM response). Web search, source reading, multi-source summarization, citations, report generation, file handling, history, and workspaces **do not exist in any form**. There is no database code, no model layer, no schema layer, and no test of any kind.

Three findings make the system non-functional as written, independent of any missing features:

1. **A live API key is hardcoded in plaintext in `app/agents/supervisor.py:8`**, and it is the wrong key for the endpoint it is sent to.
2. **`app/tasks/research_task.py:15-18` creates and closes a new event loop per task while reusing a module-level `aiogram.Bot` whose HTTP session is bound to the first loop.** Only the first query per worker process can succeed.
3. **`openai` is imported in `app/agents/supervisor.py:3` but is absent from `requirements.txt`.** A clean environment cannot start this application.

The `/health` endpoint returns `{"status": "ok"}` unconditionally, so none of the above is observable from outside the container.

---

## 2. Current Architecture

Actual implementation, derived from the call graph and source:

```
Telegram Bot API
      │  (long polling, getUpdates — no webhook)
      ▼
┌──────────────────────────────────────────────────────────────┐
│ PROCESS 1 — FastAPI + aiogram  (app/main.py)                 │
│                                                              │
│  startup event ──► asyncio.create_task(start_polling)  ──────┼──► dp.start_polling(bot)
│                                                              │
│  Dispatcher  (app/bot/dispatcher.py)                         │
│    Bot(token=settings.BOT_TOKEN, parse_mode=HTML)  ◄── .env  │
│    Dispatcher()                                              │
│                                                              │
│  Handlers  (app/bot/handlers.py)                             │
│    @dp.message(Command("start"))  ─► start_command()         │
│    @dp.message()                   ─► research_handler()     │
│                                        │                     │
│  GET /health ─► {"status":"ok"}  (static, checks nothing)    │
└───────────────────────────┬──────────────────────────────────┘
                            │ process_research.delay()
                            ▼
                    ┌───────────────┐
                    │  Redis        │  redis://localhost:6379/0
                    │  broker       │  (from .env REDIS_URL)
                    └───────┬───────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│ PROCESS 2 — Celery worker  (app/core/celery_app.py)          │
│                                                              │
│  Celery("research_bot", broker=REDIS_URL, backend=REDIS_URL) │
│    include=["app.tasks.research_task"]                       │
│                                                              │
│  process_research(user_id, query)   (research_task.py)       │
│    └─► Supervisor()                                         │
│         └─► analyze_query(query) ──► POST api.groq.com       │
│              OpenAI SDK, model="llama-3.3-70b-versatile", JSON mode │
│    └─► asyncio.new_event_loop() ... bot.send_message()  ✗    │
└──────────────────────────────────────────────────────────────┘

  ┌───────────────  NOT CONNECTED  ────────────────┐
  │ PostgreSQL 16 (docker-compose)   DATABASE_URL  │  declared,
  │ redis-py, asyncpg, sqlalchemy, httpx,          │  zero usage
  │ python-dotenv  in requirements.txt             │
  └────────────────────────────────────────────────┘
```

### Entry points

| Entry point | File | Notes |
|---|---|---|
| ASGI application | `app/main.py` | No `if __name__ == "__main__"` runner, no documented start command |
| Celery worker | `app/core/celery_app.py` | No Procfile, no Makefile, no documented start command |

### Process topology

Two processes are required but neither is containerized:

- **Process 1** — FastAPI + aiogram. Serves `/health` and runs the Telegram poller.
- **Process 2** — Celery worker. Performs the LLM call and sends the reply.

Both processes must import `app.core.config`, which instantiates `Settings()` at import time. A missing environment variable therefore crashes process startup rather than producing a configuration error.

### Absent infrastructure

No `Dockerfile`, no `Procfile`, no `Makefile`, no `README`, no `CONTRIBUTING`, no `pyproject.toml`, no CI configuration, no `.dockerignore`, no `.gitignore`, no `.env.example`, no `alembic.ini`, no test directory.

---

## 3. Implemented Features

### 3.1 Telegram — 🟡 partially implemented

| Capability | Status | Evidence |
|---|---|---|
| Bot initialization | ✅ | `app/bot/dispatcher.py:6-9` |
| Long polling | ⚠️ | `app/main.py:19-24`, fire-and-forget task, no restart on failure |
| Webhook | ❌ | Not present. No secret-token validation, no HTTPS endpoint |
| `/start` command | ✅ | `app/bot/handlers.py:9-13` |
| Command handling (general) | ❌ | Only `Command("start")`; any other command falls into the catch-all |
| Text message handling | ⚠️ | `app/bot/handlers.py:15` — no `F.text` filter, so `message.text` is `None` for photos/stickers |
| Reply mechanism | ✅ | `message.answer(...)` at line 18; `bot.send_message(...)` at `research_task.py:17` |
| User identification | ⚠️ | `message.from_user.id` used as `chat_id`; no DB user record, no allowlist, no `from_user` None-guard |
| Conversation/session handling | ❌ | No persistence, no memory, no per-user state anywhere |
| Error handling | ❌ | No `@dp.errors()` handler, no try/except in any handler |
| Markdown/HTML escaping | ❌ | `ParseMode.HTML` set globally but output is `**markdown**`, never escaped or split |

### 3.2 AI / LLM — 🟡 minimal

| Capability | Status | Evidence |
|---|---|---|
| Provider SDK | ⚠️ | `openai` SDK against Groq's OpenAI-compatible endpoint (`supervisor.py:7-10`) |
| Groq integration | ✅ | Configured — `GROQ_API_KEY` with `https://api.groq.com/openai/v1` |
| OpenRouter integration | ❌ | Absent. A key *shaped* like an OpenRouter key is present but sent to the provider |
| Model selection | ❌ | Hardcoded `self.model = "llama-3.3-70b-versatile"` (`supervisor.py:14`) |
| Prompt construction | ⚠️ | F-string prompt, no system role, user text interpolated raw |
| System/user separation | ❌ | Single `{"role": "user"}` message only |
| Conversation context | ❌ | No history, no memory, single-turn |
| Token/context management | ❌ | No `max_tokens`, no truncation, no budgeting |
| Streaming | ❌ | Absent |
| Timeout | ❌ | Absent — SDK default (600s) applies |
| Retry / backoff | ❌ | Absent (SDK internal retries only) |
| Rate-limit (429) handling | ❌ | Absent |
| Fallback models | ❌ | Absent |
| Response validation | ❌ | Bare `json.loads`, then direct `dict` access |

### 3.3 Database — ❌ not implemented

Zero database code exists. Verified by keyword scan across `app/` for `sqlalchemy`, `asyncpg`, `create_engine`, `sessionmaker`, `BaseModel`, `alembic`, `migration` — **no matches** outside unrelated string occurrences.

- `DATABASE_URL` declared in `app/core/config.py:6` and set in `.env` — never imported anywhere.
- `asyncpg`, `sqlalchemy` pinned in `requirements.txt:6-7` — never imported.
- `postgres:16-alpine` service in `docker-compose.yml:10-20` — nothing connects to it.
- No tables, models, relationships, indexes, constraints, or migrations.
- No user, conversation, message, research, or source storage.

### 3.4 Research functionality — ❌ none exists

| Capability | Status |
|---|---|
| Web search | ❌ |
| URL fetching | ❌ |
| HTML extraction | ❌ |
| Content cleaning | ❌ |
| Source ranking | ❌ |
| Source deduplication | ❌ |
| Summarization | ❌ |
| Multi-source analysis | ❌ |
| Citation generation | ❌ |
| Fact checking | ❌ |
| Report generation | ❌ |

`Supervisor.generate_final_answer` (`app/agents/supervisor.py:46-65`) is the only trace of a synthesis stage. It has **0 inbound and 0 outbound call edges** in the graph — verified dead code. Nothing fetches, reads, or cites any source; the sub-questions are generated and then discarded.

### 3.5 Infrastructure — 🟡 partial

| Item | Status | Evidence |
|---|---|---|
| Docker Compose | ⚠️ | Redis + Postgres only; **no bot service, no worker service** |
| Dockerfile | ❌ | Absent |
| Env configuration | ⚠️ | `app/core/config.py`, works but fragile |
| `.env.example` | ❌ | Absent |
| `.gitignore` | ❌ | **Absent** — `.env` and `venv/` are unprotected |
| Secrets management | ❌ | One key hardcoded in source; no rotation, no secret store |
| Logging | ⚠️ | `logging.basicConfig` called twice; f-strings; full user text logged |
| Monitoring | ❌ | Absent |
| Health checks | ⚠️ | `/health` is static; no Compose healthchecks; no dependency probing |
| DB migrations | ❌ | Absent |
| Background jobs | ✅ | Celery + Redis broker |
| Redis / Celery | ⚠️ | Present but unconfigured for reliability (see P0-5) |
| Tests | ❌ | Zero |
| CI / linting | ❌ | No config files |

---

## 4. Current Message Flow

Traced from source and confirmed against graph edges `research_handler → process_research` and `process_research → Supervisor.analyze_query`.

```
1.  Telegram message
2.  app/main.py:15-17         on_startup() → asyncio.create_task(start_polling())
3.  app/main.py:22            await dp.start_polling(bot)      [long polling, no webhook]
4.  app/bot/dispatcher.py:6   bot = Bot(settings.BOT_TOKEN, parse_mode=HTML)
5.  app/bot/handlers.py:9     @dp.message(Command("start")) → start_command()   [/start only]
6.  app/bot/handlers.py:15    @dp.message() → research_handler()                [catch-all, no filter]
      ├─ handlers.py:17       logging.info(f"...: {message.text}")   ← full user text logged
      ├─ handlers.py:18       await message.answer("⏳ Analysing your question…")
                                    ↑ the original pre-Phase-1 string was in Persian; the
                                      finding below is about format, not language
      └─ handlers.py:19-22    process_research.delay(user_id=message.from_user.id, query=message.text)
7.  Redis broker              redis://localhost:6379/0
8.  app/tasks/research_task.py:8   supervisor = Supervisor()        [per-task instantiation]
9.  app/agents/supervisor.py:9     Supervisor.analyze_query(query)
      ├─ supervisor.py:20-34  builds f-string prompt, no system role
      ├─ supervisor.py:36-41  client.chat.completions.create(
      │                            model="llama-3.3-70b-versatile", temperature=0.3,
      │                            response_format={"type":"json_object"})
      │                            → POST https://api.groq.com/openai/v1/chat/completions
      │                            NO timeout, NO max_tokens, NO retry, NO auth fallback
      └─ supervisor.py:43     result = json.loads(...)            ← unguarded
10. research_task.py:12       f-string builds "**Research result:**..."  ← Markdown, sent as HTML
                                    ↑ the original pre-Phase-1 label was in Persian; the defect
                                      is the Markdown-under-HTML mismatch, not the language
11. research_task.py:15-16    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
12. research_task.py:17       loop.run_until_complete(bot.send_message(chat_id=user_id, text=...))
13. research_task.py:18       loop.close()                        ← closes loop, leaks HTTP session
```

### Answers to the specific questions

- **Which function receives it?** `app/bot/handlers.py:research_handler` (registered at line 15).
- **Which service processes it?** None. There is no service layer. `app/tasks/research_task.py:process_research` is the task body and `app/agents/supervisor.py:Supervisor` is the only logic holder.
- **How is the user identified?** `message.from_user.id` (`handlers.py:20`). Not persisted, not validated, no allowlist. This value is then used as `chat_id` at `research_task.py:17` — correct in a private chat, **broken in any group** (see H-1).
- **How is conversation context retrieved?** It isn't. `analyze_query(query)` receives exactly one string. No history, no memory, no DB.
- **How is the LLM called?** `client.chat.completions.create` at `supervisor.py:36-41` using the `openai` SDK against `https://api.groq.com/openai/v1`.
- **How is the response generated?** `json.loads` of the model output into a dict, then string-interpolated into a Persian Markdown template at `research_task.py:12`.
- **How is it sent back?** `bot.send_message(chat_id=user_id, text=final_answer)` at `research_task.py:17`, inside a manually created event loop.
- **What happens on error?** Nothing recoverable. There is exactly **one** `try`/`except` in the entire codebase (`main.py:20-23`, which only guards polling startup). If the provider call raises, Celery marks the task `FAILURE`, no message is sent, and the user's "⏳ analyzing…" message is never resolved. If Redis is down, `process_research.delay()` raises inside the aiogram handler with no reply. The user is left hanging with no error path in either case.

---

## 5. Security Findings

**SECRET FOUND IN FILE: `app/agents/supervisor.py`** — a full API key literal in `api_key=` at line 8. It is `sk-or-v1-`-prefixed (OpenRouter key format) but is sent to the provider's OpenAI-compatible base URL (line 9), whose keys are `sk-`-prefixed. Two distinct problems: the credential is exposed in plaintext source, and it cannot authenticate to the configured endpoint. The correct key exists in `.env` as `GROQ_API_KEY` and is **never read by any code**.

**SECRET FOUND IN FILE: `.env`** — contains a real-format `BOT_TOKEN` and a real-format provider API key. Values were not printed and were not tested for validity.

### Findings

| ID | Severity | Finding | Location |
|---|---|---|---|
| S-1 | CRITICAL | Plaintext API key literal in source, with a key/endpoint mismatch that guarantees authentication failure | `app/agents/supervisor.py:7-10` |
| S-2 | HIGH | No `.gitignore`. Not currently a git repository, so nothing is committed today — but a single `git init && git add .` publishes a live Telegram bot token, an API key, and the entire `venv/`. No `.env.example` documents the required variables | repo root |
| S-3 | MEDIUM | Unauthenticated `/health`. Low information disclosure alone, but it is the only endpoint and has no rate limiting | `app/main.py:11-13` |
| S-4 | MEDIUM | PostgreSQL published with default credentials in plaintext; Redis published with no authentication. Both bind to all interfaces by default. Acceptable for local dev, unsafe on a shared or cloud host | `docker-compose.yml:6-7, 13-17` |
| S-5 | MEDIUM | Prompt injection, unmitigated. Raw user text interpolated into a prompt via f-string, no system message, no delimiter, no instruction to ignore embedded directives. Model output is rendered back to the user. Blast radius is low today (one JSON decomposition call), but the moment web-fetched content enters a prompt this becomes a real injection vector, and the architecture has no defense in place | `app/agents/supervisor.py:23` |

### Attack surface not currently present

No SQL, so no SQL injection surface. No `subprocess` / `os.system` / `eval` / `exec`, so no command injection. No URL fetching, so no SSRF surface *yet*. No deserialization of untrusted data (`json.loads` only). No file-path handling from user input, so no arbitrary file access. No auth layer to bypass.

### Not verified

**NOT VERIFIED — insufficient evidence in repository.** Whether the exposed credentials are currently active cannot be determined without network calls, which are out of scope for a read-only audit. Whether they were ever committed cannot be determined: there is no `.git` directory and therefore no history to inspect.

---

## 6. Bugs & Technical Issues

### 6.1 CRITICAL

#### C-1 · Hardcoded, mismatched API key
**File:** `app/agents/supervisor.py:7-10` (module level)
**Function/class:** module-level `client`

An OpenRouter-format key is hardcoded and pointed at the provider's base URL. The key is in plaintext source, it is not the key configured in `.env`, and it will be rejected by the endpoint.

- **Why it matters:** guaranteed authentication failure on every LLM call, plus credential exposure.
- **Fix:** read `settings.GROQ_API_KEY` (already defined at `config.py:7`), add `openai` to `requirements.txt`, rotate the exposed key, and scrub it from source history if this ever reaches a shared repo.

#### C-2 · `openai` missing from `requirements.txt`
**File:** `requirements.txt:1-10`

`supervisor.py:3` imports `openai`; `requirements.txt` does not list it. It is present in `venv/` (installed ad hoc) but a clean install cannot run this app.

- **Why it matters:** unreproducible environment, guaranteed `ModuleNotFoundError` on deploy.
- **Fix:** add `openai` with a pinned version.

#### C-3 · Event-loop lifecycle bug breaks all but the first query per worker
**File:** `app/tasks/research_task.py:15-18`
**Function/class:** `process_research`

A new event loop is created per task and closed, while `bot` is a module-level singleton (`dispatcher.py:6`). Verified in the installed aiogram that `Bot.session` is a single `AiohttpSession` created at `Bot.__init__` (`aiogram/client/bot.py:288-293`), whose underlying aiohttp `ClientSession` is created **lazily and cached** (`aiogram/client/session/aiohttp.py:133-146` — recreated only if `None` or `.closed`). The session is therefore bound to the first task's loop, which line 18 closes.

- **Consequences:** the first task leaks an unclosed `ClientSession` (leaked sockets, `ResourceWarning`); every subsequent task in the same prefork worker reuses a session bound to a dead loop and fails on loop-bound operations. `bot.session.close()` is never called, and the intended `async with bot` lifecycle (`bot.py:354-361`) is unused.
- **Why it matters:** the product's single core feature works approximately once per worker process.
- **Fix:** create one long-lived loop per worker process via Celery's `worker_process_init` signal (or run an asyncio loop in a dedicated thread/greenlet) and never close it per task; close the session on `worker_process_shutdown`.

#### C-4 · HTML parse mode with unescaped, unsplit LLM output
**Files:** `app/bot/dispatcher.py:8`, `app/tasks/research_task.py:12, 17`
**Function/class:** `process_research`

`dispatcher.py:8` sets `default=DefaultBotProperties(parse_mode=ParseMode.HTML)`, but `research_task.py:12` builds a `**bold**` Markdown string and line 17 sends it with no `parse_mode` override. Two defects: the `**` markers render literally instead of bold, and any `<` or `&` in the model's output causes Telegram to reject the message with `400 can't parse entities`, which — combined with the total absence of error handling (C-5) — means silent message loss. Separately, there is no length guard against Telegram's 4096-character limit and no `max_tokens` cap on generation (`supervisor.py:36-41`), so an over-long summary is equally likely to be rejected.

- **Fix:** pick one parse mode, escape via `html.escape` or send with `parse_mode=None`, and chunk with aiogram's `text.split_text` utility.

### 6.2 HIGH

#### H-1 · `from_user.id` used as `chat_id` breaks group usage
**Files:** `app/bot/handlers.py:20-21`, `app/tasks/research_task.py:17`
**Function/class:** `research_handler`, `process_research`

In a private chat, `message.chat.id` and `message.from_user.id` are equal and the code works. In a group or supergroup, `chat.id` is negative and `from_user.id` is the sender, so the reply is addressed to the user's direct messages and fails with `403 bot can't initiate conversation with a user` for anyone who has not started the bot. Because the acknowledgement at `handlers.py:18` *is* sent to the group correctly, the visible symptom is a bot that says "analyzing…" in the channel and then never answers.

- **Fix:** pass `chat_id=message.chat.id` alongside the user id.

#### H-2 · No error handling anywhere in the request path
**Files:** `app/bot/handlers.py`, `app/tasks/research_task.py`, `app/main.py:19-24`

The only `try`/`except` in the repository is `main.py:20-23`. Consequences: `process_research.delay()` is unguarded (a Redis outage raises into the aiogram handler with no reply and no user-visible error); the task body has no `try/except`, so any LLM, JSON, or Telegram failure ends in a silent `FAILURE` state; the user permanently sees the "⏳ analyzing…" placeholder. There is no `@dp.errors()` handler, no `autoretry_for`, and no failure notification.

- **Fix:** add an aiogram errors handler, wrap the task body, and always send a terminal message to the user.

#### H-3 · No timeouts, no retries, no token limits on LLM calls
**Files:** `app/agents/supervisor.py:36-41, 59-63`, `app/core/celery_app.py:11-17`
**Function/class:** `Supervisor.analyze_query`, `Supervisor.generate_final_answer`

No `timeout`, no `max_tokens`, and no retry policy are passed to the SDK, and `celery_app.py` sets no `task_time_limit` / `task_soft_time_limit` / `task_acks_late` / `task_reject_on_worker_lost` / `broker_connection_retry_on_startup` / prefetch tuning. A hung upstream connection can occupy a prefork worker slot indefinitely, and an unbounded `max_tokens` is an unbounded cost exposure on a public bot endpoint.

- **Fix:** explicit `timeout=` and `max_tokens=` on every call, `task_time_limit` on the task, and bounded `autoretry_for` with backoff.

#### H-4 · Unguarded parsing of untrusted model output
**Files:** `app/agents/supervisor.py:43`, `app/tasks/research_task.py:9, 12`
**Function/class:** `Supervisor.analyze_query`, `process_research`

`json.loads(response.choices[0].message.content)` is called with no `try`, no null-check on `content`, and no schema validation; `research_task.py:12` then indexes `analysis['summary']` and `analysis['sub_questions']` directly. A malformed or truncated response raises `JSONDecodeError`, `TypeError`, or `KeyError` and, per H-2, produces no user-visible outcome.

- **Fix:** a Pydantic response model with defaults, plus validation at the boundary.

#### H-5 · `/health` is a static literal and masks total failure
**File:** `app/main.py:11-13`
**Function/class:** `health_check`

Returns `{"status": "ok", "service": "fastapi"}` unconditionally. It does not check whether the polling task at line 17 is still alive, whether Redis accepts connections, or whether the LLM endpoint is reachable. If polling raises, `main.py:23-24` logs the error and gives up permanently — the bot is dead while health stays green.

- **Fix:** track task liveness and probe Redis; return 503 on failure.

#### H-6 · No graceful shutdown, deprecated startup hook
**File:** `app/main.py:15-18`
**Function/class:** `on_startup`, `start_polling`

`asyncio.create_task(start_polling())` fires without retaining a handle and without registering shutdown; `dp.stop_polling()` is never called, so the poller is not stopped cleanly on SIGTERM and in-flight updates can be lost mid-restart. `@app.on_event("startup")` is deprecated in the pinned FastAPI 0.115.6 in favour of the `lifespan` context manager.

- **Fix:** move to `lifespan`, await startup, and close the dispatcher on shutdown.

#### H-7 · Catch-all handler without a content-type filter
**File:** `app/bot/handlers.py:15`
**Function/class:** `research_handler`

`@dp.message()` is registered with no filter, so it also receives commands other than `/start`, photos, stickers, voice notes, and location payloads. For all non-text messages `message.text` is `None`, and lines 19-22 enqueue a research task with `query=None`, producing an "analyzing…" reply to a photo and a meaningless LLM call.

- **Fix:** add `F.text` and register unknown commands separately.

#### H-8 · Docker Compose cannot run the application
**File:** `docker-compose.yml`

Only `redis` and `postgres` are defined. There is no `bot` service and no `celery worker` service, so the two processes the product actually consists of are not containerized; there is no `Dockerfile` at all. The compose file also omits healthchecks, uses the obsolete top-level `version: '3.8'` key, and does not wire `.env` into the containers.

- **Fix:** add a Dockerfile and `bot` + `worker` services with healthchecks and dependency conditions.

### 6.3 MEDIUM

#### M-1 · Dead code: the entire synthesis stage
**File:** `app/agents/supervisor.py:46-65`
**Function/class:** `Supervisor.generate_final_answer`

Zero call edges in the graph. The task named `process_research` (`research_task.py:7`) performs only question decomposition and never answers the sub-questions it generates. This is the single largest gap between the code's naming and its behaviour.

#### M-2 · No database layer
**Files:** `app/core/config.py:6`, `requirements.txt:6-7`, `docker-compose.yml:10-20`

`DATABASE_URL`, `asyncpg`, `sqlalchemy`, and the `postgres` service are all present and entirely unused. No models, no session management, no migrations, no schema.

#### M-3 · No conversation memory
**File:** `app/agents/supervisor.py:16`
**Function/class:** `Supervisor.analyze_query`

Every message is processed statelessly. `analyze_query` receives one string. A user cannot ask a follow-up question, and there is no history to store.

#### M-4 · Module-level singletons prevent testability
**Files:** `app/agents/supervisor.py:7`, `app/bot/dispatcher.py:6`, `app/tasks/research_task.py:8`

`client` and `bot` are constructed at import time from global settings. There is no dependency injection, no factory, and no seam to substitute a fake, so neither the LLM nor the Telegram transport can be tested without monkeypatching module globals. `Supervisor()` is also re-instantiated per task.

#### M-5 · Unused imports and unused dependencies
**Files:** `app/agents/supervisor.py:1, 4`, `requirements.txt`

`supervisor.py:1` imports `os` and `supervisor.py:4` imports `settings`; both appear exactly once in the file — the import line itself — confirming they are unused. `settings` being unused *is* the direct cause of C-1. In `requirements.txt`, `httpx`, `redis`, `asyncpg`, `sqlalchemy`, and `python-dotenv` have no importing module anywhere in `app/` (the last is also redundant with `pydantic-settings`).

#### M-6 · Configuration fragility
**File:** `app/core/config.py`
**Class:** `Settings`

`env_file = ".env"` at line 10 is resolved by pydantic-settings relative to the process working directory, so the app fails to find its config when launched from anywhere other than the repo root — a guaranteed problem once a container sets a different `WORKDIR`. All four fields are required with no defaults and no validators, so a single missing variable raises `ValidationError` at *import* time of `app.core.config`, which `app/main.py:3` imports during application construction — the process dies with a stack trace instead of a configuration error message. `class Config` is the pydantic-v1 style, still honoured via a deprecation shim in the pinned pydantic-settings 2.7.0.

#### M-7 · Logging is duplicated, unstructured, and leaks user content
**Files:** `app/main.py:7`, `app/bot/handlers.py:7, 17`

`logging.basicConfig(level=logging.INFO)` is called in both modules. All logging uses eager f-string interpolation rather than lazy `%s` args. `handlers.py:17` logs the user's complete message text — user content in logs with no retention policy. There are no correlation or task IDs, no log level discipline, no centralization, and — per H-2 — no logging of task failures at all.

#### M-8 · No rate limiting or cost control
**Files:** `app/bot/handlers.py`, `app/core/celery_app.py:11-17`

No per-user throttle, no global concurrency cap, no queue-depth guard. Any user can enqueue an unbounded number of billable LLM tasks, and there is no flood-control handling for Telegram 429 responses on the send path.

#### M-9 · Celery result backend misused
**File:** `app/core/celery_app.py:6-7`

`backend` points at the same Redis URL as the broker, and `task_ignore_result` is not set. No task result is ever read, so Redis accumulates result keys indefinitely with no TTL.

#### M-10 · Untyped boundary contract
**File:** `app/agents/supervisor.py:16, 46`
**Function/class:** `Supervisor.analyze_query`, `Supervisor.generate_final_answer`

`analyze_query` is annotated `-> dict` and `generate_final_answer` takes a bare `list`. There is no Pydantic model, no schema module, and no validation anywhere — the entire contract between the agent and the task is an unvalidated dictionary. This is inconsistent with the otherwise reasonable use of Pydantic in `config.py`.

### 6.4 LOW

| ID | File | Issue |
|---|---|---|
| L-1 | `.python-version`, `__pycache__/` | Environment drift: `.python-version` pins `3.12.0` and `venv/lib/python3.12` matches, but `__pycache__` contains `cpython-314` artifacts, indicating the code has also been run under 3.14 |
| L-2 | `app/bot/handlers.py:12-13, 18`, `app/tasks/research_task.py:12` | User-facing strings are hardcoded Persian literals. No i18n layer, which will need replacing for the planned SaaS |
| L-3 | `app/main.py:9`, `app/bot/handlers.py:12`, `app/core/celery_app.py:5` | Naming is inconsistent across layers: the FastAPI app is `"Research Bot Core"`, the user-facing greeting says `"DeepVoyager"`, and the Celery app is `"research_bot"` |
| L-4 | `app/agents/supervisor.py:54` | `chr(10)` used inside an f-string to work around nested-quote escaping — a readability smell indicating prompt templates need a proper abstraction |
| L-5 | `app/core/celery_app.py:15` | `timezone="Asia/Tehran"` hardcoded rather than configured |
| L-6 | repo root | No `.dockerignore`, `Makefile`, `README`, `CONTRIBUTING`, `pyproject.toml`, editor/formatter config, or dependency hashes |
| L-7 | `app/bot/dispatcher.py:10` | A `Dispatcher` is constructed that the Celery worker also imports and instantiates (via `research_task.py:3`) but never uses |
| L-8 | `app/bot/handlers.py:11, 17` | `message.from_user.id` accessed without a `None` guard. Low risk for ordinary `Message` updates, but the code has no defensive posture anywhere |

---

## 7. Code Quality Findings

**Project organization.** The directory layout is conventional and, importantly, *correct in intent*: `bot/` for Telegram, `core/` for config and Celery, `agents/` for LLM logic, `tasks/` for background work, with a thin `main.py`. The problem is that the layering is nominal rather than real — there is no service layer, so `research_task.py` composes the agent, formats the response, manages an event loop, and talks to Telegram all in 18 lines. `app/bot/` should arguably be `app/telegram/`, but that is cosmetic next to the missing middle tier.

**Separation of concerns.** Poor in the places that matter. Presentation formatting (`research_task.py:12`), transport lifecycle (`research_task.py:15-18`), and LLM invocation (`supervisor.py`) are interleaved in one function. A formatting change and a transport change require editing the same 4 lines.

**SOLID.** SRP is violated in `process_research` and, more seriously, in AiohttpSession lifecycle management inside it. OCP is nominally satisfied by the `Supervisor` class but defeated by the hardcoded `self.model` (`supervisor.py:14`) — adding a provider requires editing the class rather than extending it. DIP is not attempted: no module depends on an abstraction, everything reaches for a module-level global. LSP and ISP have no applicable surface.

**Type safety.** Partial and inconsistent. Annotations exist on the `Settings` fields and on `Supervisor` method signatures, but the most important boundary — the LLM response — is `dict`. There is no `py.typed`, no mypy config, no runtime validation, and no CI gate. Type hints here are documentation, not enforcement.

**Naming.** Reasonable at the local level (`analyze_query`, `process_research`, `on_startup`) but inconsistent at the product level (L-3), and actively misleading in one place: `process_research` performs no research (M-1).

**Duplication.** Minimal, which is a genuine positive — there is very little code to duplicate. The one real instance is the duplicated `logging.basicConfig` (M-7).

**Dependency management.** Weak. Ten pinned direct dependencies, of which five are entirely unused (M-5) and one required at runtime is missing (C-2). `requirements.txt` is a flat list with no separation of direct/transitive/dev dependencies, no hashes, and no lockfile. The venv contains packages installed outside the declared set, which is how C-2 went unnoticed.

**Configuration management.** A single `Settings` class is the right shape, but as noted in M-6 it is CWD-dependent, has no defaults or validation, uses the legacy `class Config` form, and is bypassed entirely by the one place that actually needs a secret (C-1). Two competing sources of truth exist for the provider key: `.env` and a source literal, and the code uses the wrong one.

**Logging.** See M-7. The one real gap is that the background task — where essentially all failure modes live — produces no logs at all.

**Testability.** Effectively zero. Module-level singletons (M-4) mean both external boundaries must be monkeypatched; there are no seams, no fixtures, and no test runner in the dependency set.

**Maintainability.** Low, and the limiting factor is not style but that the system cannot currently complete a request. Beyond that, the three genuine debt items are: a task named for a feature that does not exist (M-1), an entire provisioned data tier that is never touched (M-2), and secrets/config handled in two places at once (C-1, M-6).

---

## 8. Testing Gaps

**Existing tests: none.** No `test_` file, no `tests/` directory, no `conftest.py`, and no test framework in `requirements.txt` (no `pytest`, `pytest-asyncio`, `respx`, `fakeredis`, or `testcontainers`). Confirmed by keyword scan for `pytest`, `unittest`, and `test_` across `app/`: no matches.

| Area | Test coverage | Gap |
|---|---|---|
| Telegram handlers | none | `start_command`, `research_handler` completely untested |
| Dispatcher / bot construction | none | Parse-mode and token wiring untested |
| Celery task | none | `process_research` untested, including the event-loop defect in C-3 |
| LLM / prompt | none | `analyze_query` and `generate_final_answer` untested |
| Config loading | none | No verification that required vars resolve |
| Database | none | No layer to test |
| Health endpoint | none | Trivial but unverified |
| Docker / compose | none | No build or smoke test |

### Critical functionality with no tests

In rough order of blast radius:

1. `app/tasks/research_task.py:process_research` — the entire request-completion path, including the event-loop lifecycle flagged as C-3. This is the single highest-value test in the project, and it is a pure unit test with a fake `bot` and a fake LLM client.
2. `app/agents/supervisor.py:Supervisor.analyze_query` — LLM response parsing, including malformed JSON, missing keys, and null content (H-4).
3. `app/bot/handlers.py:research_handler` — filter behaviour, `from_user` handling, and the group `chat_id` bug (H-1, H-7).
4. `app/core/config.py:Settings` — that all four variables resolve, guarding against the CWD and missing-var failure modes in M-6.
5. Message formatting and the 4096/HTML constraints (C-4) — a pure function once extracted.
6. An end-to-end smoke test asserting that a synthetic update produces exactly one Telegram reply.

There is no CI configuration, so nothing prevents regressions even after tests are added.

---

## 9. Roadmap Status

| Phase | Status | Completed | Partial | Blockers / dependencies |
|---|---|---|---|---|
| **1 — Core Infrastructure** | 🟡 Partial | FastAPI app, aiogram polling, Celery + Redis, config module, compose for Redis/Postgres | Logging (basic), health endpoint (non-functional) | C-1, C-2, C-3, C-4, H-5, H-6, H-8, M-6, M-7; no Dockerfile, no tests, no CI, no migrations |
| **2 — AI Chat MVP** | ❌ Not started | One LLM call returns text to Telegram | — | No chat semantics, no context/memory, no streaming, no model selection, no fallback, no rate limiting, no `/help`, no error UX. Stateless single-turn decomposition is not a chat MVP |
| **3 — Research Engine** | ❌ Not started | `generate_final_answer` stub exists but is unwired (M-1) | — | No search, no fetch, no extraction, no cleaning, no ranking, no dedup, no summarization, no citations, no reports. Blocked on Phase 2 hardening and the Phase 1 data tier |
| **4 — File Intelligence** | ❌ Not started | — | — | No download, parsing, or upload handling of any kind. Blocked on Phase 3 pipeline and an object storage decision |
| **5 — Research Workspace** | ❌ Not started | — | — | No database layer at all (M-2). Blocked on Phase 1 migrations and Phase 2/3 schemas |
| **6 — Agent System** | 🟡 Sketched | Naming and file placement (`app/agents/supervisor.py`) | A two-stage Supervisor design is *implied* by the code | No orchestration, no tool registry, no planner/executor split, no agent loop, no state machine. Stage 2 is dead code. Blocked on Phase 3 tools to orchestrate |
| **7 — Premium Features** | ❌ Not started | — | — | No user accounts, no tiers, no quotas. Blocked on Phase 5 |
| **8 — SaaS / Web Dashboard** | ❌ Not started | — | — | No auth, no multi-tenancy, no REST API (one health route), no frontend, no i18n. Blocked on Phases 1–5 |
| **9 — Monetization** | ❌ Not started | — | — | No billing, no payment integration, no entitlement model. Blocked on Phase 7 |

**Summary:** the repository has reached roughly the end of Phase 1 *in intent* and about 5% of Phase 2. Phases 3–9 have no implementation, and the Phase 6 naming is aspirational rather than functional.

---

## 10. Recommended Next Steps

Sequenced by dependency, not by severity alone.

1. **Make the current path actually work, then prove it with a test.** Fix C-1 through C-4 and H-1/H-2 as a single change, and add one unit test for `process_research` that runs the task twice in the same process — that test alone would have caught the event-loop defect. Until this is done, no feature work can be validated, because there is no reliable signal that a request completes.

2. **Establish the data tier and the configuration contract.** Add SQLAlchemy async models plus Alembic for users, conversations, and messages (M-2), wire `DATABASE_URL` into a real session factory, add health checks that probe Redis and Postgres (H-5), and add a Dockerfile plus `bot`/`worker` compose services (H-8). This unblocks Phase 5 and makes every later feature persistable.

3. **Turn the skeleton into an actual chat MVP before any research work.** Add a real service layer with dependency injection, conversation memory, `/help` and error-feedback commands, `F.text` filtering, per-user rate limiting, and model selection with a fallback (Phase 2). This produces the first genuinely testable, user-facing capability — and only after it is solid does wiring search/fetch/summarize/cite into the existing `generate_final_answer` stub represent progress rather than more scaffolding.

---

## 11. P0/P1/P2/P3 Priority List

### P0 — Critical: security and non-functional

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P0-1 | Remove the hardcoded key; read `settings.GROQ_API_KEY`; add `openai` to `requirements.txt`; **rotate the exposed key and the `.env` bot token** | C-1, C-2 — plaintext credential in source, wrong key for the endpoint, and the app cannot start in a clean environment | `app/agents/supervisor.py:1-10`, `requirements.txt` | — |
| P0-2 | Add `.gitignore` (`.env`, `venv/`, `__pycache__/`) and `.env.example` | One `git init && git add .` publishes a live bot token, an API key, and the whole venv | repo root | — |
| P0-3 | Fix the event-loop lifecycle: one long-lived loop per worker process; close the aiogram session on shutdown | C-3 — the core feature succeeds roughly once per worker process and leaks sockets | `app/tasks/research_task.py:15-18`, `app/bot/dispatcher.py:6` | P0-1 |
| P0-4 | Resolve the parse-mode conflict: one parse mode, `html.escape` or `parse_mode=None`, and `text.split_text` for the 4096 limit | C-4 — malformed or over-long LLM output silently drops every reply | `app/bot/dispatcher.py:8`, `app/tasks/research_task.py:12,17` | — |
| P0-5 | Add error handling across the request path: `@dp.errors()`, `try/except` in the task, and a terminal user-facing failure message; add `timeout`, `max_tokens`, and bounded `autoretry_for` with `task_time_limit` | H-2, H-3 — every failure mode is currently silent and the user is left hanging; unbounded tokens are unbounded cost | `app/bot/handlers.py`, `app/tasks/research_task.py`, `app/agents/supervisor.py:36,59`, `app/core/celery_app.py:11-17` | P0-3 |

### P1 — Architecture and correctness

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P1-1 | Pass `chat_id=message.chat.id`; add `F.text` filter and a `from_user` guard | H-1, H-7, L-8 — the bot answers into the void in any group and mis-handles every non-text message | `app/bot/handlers.py:15-22` | — |
| P1-2 | Make `/health` real: track the polling task, probe Redis and Postgres, return 503 on failure; move startup to `lifespan` and add graceful shutdown | H-5, H-6 — a dead bot currently reports healthy; shutdown loses in-flight updates | `app/main.py:11-24` | — |
| P1-3 | Introduce a service layer with dependency injection; stop constructing `bot` and `client` at import time | M-4 — nothing is testable, and configuration has two competing sources of truth | `app/bot/dispatcher.py`, `app/agents/supervisor.py:7`, `app/tasks/research_task.py:8` | P0-5 |
| P1-4 | Add a Pydantic schema for the LLM response and validate at the boundary; remove the dead `generate_final_answer` or wire it in | H-4, M-1, M-10 — the agent/task contract is an unvalidated dict, and the synthesis stage does not exist | `app/agents/supervisor.py:16,43,46-65`, `app/tasks/research_task.py:9,12` | P1-3 |
| P1-5 | Add per-user rate limiting, Celery concurrency and prefetch caps, `task_ignore_result`, and flood-control handling | M-8, M-9 — unbounded billable work from any single user; unbounded Redis growth | `app/bot/handlers.py`, `app/core/celery_app.py:11-17` | P0-5 |
| P1-6 | Add Dockerfile plus `bot` and `worker` compose services with healthchecks; stop publishing Redis/Postgres with default credentials | H-8 — the application cannot be deployed as written | `docker-compose.yml`, new `Dockerfile` | P1-2 |
| P1-7 | Clean dependencies: remove the five unused packages, prune unused imports | M-5 — `requirements.txt` misrepresents the system and hid the missing `openai` | `requirements.txt`, `app/agents/supervisor.py:1,4` | P0-1 |

### P2 — MVP requirements (Phase 2)

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P2-1 | Add the data tier: SQLAlchemy async models + Alembic for users, conversations, messages; wire `DATABASE_URL` into a session factory | M-2 — a fully provisioned but entirely unused Postgres; blocks every later phase | new `app/db/`, new `alembic/` | P1-3 |
| P2-2 | Implement conversation memory and multi-turn context with token budgeting and truncation | M-3 and Phase 2's core requirement — the bot is currently stateless | `app/agents/`, new service layer | P2-1 |
| P2-3 | Add `/help`, unknown-command handling, and user feedback on task state (queued / running / failed) | H-2, H-7 — basic UX is missing entirely | `app/bot/handlers.py` | P1-1, P0-5 |
| P2-4 | Add model selection with a fallback chain, plus streaming for long answers | Phase 2 requirements; absent today | `app/agents/supervisor.py:14` | P1-3 |
| P2-5 | Fix configuration robustness: absolute `env_file` path, defaults, validators, `model_config`, and fail-fast with a readable message | M-6 — CWD-dependent config that crashes at import time | `app/core/config.py` | — |
| P2-6 | Consolidate logging: single `basicConfig`, lazy `%s` args, stop logging full user text, add task/correlation IDs, log task failures | M-7 — the background task, where all failures live, logs nothing | `app/main.py:7`, `app/bot/handlers.py:7,17`, `app/tasks/research_task.py` | P1-3 |
| P2-7 | Stand up the test harness: `pytest`, `pytest-asyncio`, `respx`, `fakeredis`; first test is `process_research` executed twice in one process | No tests exist; this is the regression net for every fix above | new `tests/`, `requirements.txt` | P0-3, P1-3 |
| P2-8 | Add CI running lint, type-check, and tests | Nothing prevents regressions | new CI config | P2-7 |

### P3 — Future improvements

| # | Task | Why | Files | Depends on |
|---|---|---|---|---|
| P3-1 | Build the research pipeline: web search → URL fetch → HTML extraction → cleaning → dedup → ranking | Phase 3; no primitive exists yet | new `app/research/` | P2-7 |
| P3-2 | Wire multi-source synthesis and citation generation into the existing `generate_final_answer` stub | Closes the largest naming/behaviour gap in the codebase | `app/agents/supervisor.py:46-65` | P3-1 |
| P3-3 | Structured report generation with persisted research and source records | Phase 3 deliverable; requires the schema from P2-1 | new `app/research/`, `app/db/models` | P2-1, P3-2 |
| P3-4 | File/document ingestion (PDF, DOCX) with size and type limits | Phase 4 | new `app/documents/` | P2-1, P3-1 |
| P3-5 | i18n layer replacing hardcoded Persian strings | Required before any SaaS surface | `app/bot/handlers.py:12,18`, `app/tasks/research_task.py:12` | P2-3 |
| P3-6 | Structured logging, metrics, and tracing | No observability beyond stdout | new | P2-6 |
| P3-7 | Agent orchestration: tool registry and planner/executor loop over the research tools | Phase 6; the `Supervisor` naming currently implies a capability that does not exist | `app/agents/` | P3-2 |
| P3-8 | `pyproject.toml`, formatter/linter config, `Makefile`, `README`, `.dockerignore`; remove obsolete compose `version` key and reconcile naming | General maintainability debt | repo root, `docker-compose.yml:1` | P2-8 |
| P3-9 | Multi-tenancy, auth, REST API, and web dashboard | Phases 7–8 | new | P2-1, P2-5, P3-5 |

---

## 12. Overall Project Readiness

| Area | Status | Evidence | Problems |
|---|---|---|---|
| Telegram | ⚠️ | `dispatcher.py`, `handlers.py`, `main.py` | Polling only; no error handler; wrong `chat_id` in groups; no content filter; parse-mode conflict; no graceful shutdown; no rate limiting; no session handling |
| Backend | ⚠️ | `main.py` (24 lines), `celery_app.py` | No service layer; no DI; module-level singletons; broken worker async model; `/health` non-functional; deprecated startup hook |
| Database | ❌ | `config.py:6`, `requirements.txt:6-7`, `docker-compose.yml:10-20` | Zero code. No models, tables, migrations, sessions, or usage of any kind |
| LLM | ⚠️ | `supervisor.py` | Hardcoded wrong key; missing dependency; no timeout/retries/`max_tokens`; no system role; no schema validation; no model selection or fallback; no streaming; no context management |
| Web Search | ❌ | — | Nothing exists. No client, no provider, no configuration |
| Research Engine | ❌ | `supervisor.py:46-65` (dead code) | Only an unwired stub. No fetch, extraction, cleaning, dedup, ranking, summarization, citation, or reporting |
| File Processing | ❌ | — | Nothing exists |
| Authentication | ❌ | — | No auth layer, no user records, no allowlist, no tenant isolation |
| Error Handling | ❌ | `main.py:20-23` (sole instance) | One bare `except` in the whole codebase. No handler-level protection, no task-level protection, no user-facing failure path, no 429 handling |
| Testing | ❌ | — | Zero tests, zero test framework, no CI |
| Docker | 🟡 | `docker-compose.yml` | Redis + Postgres only; no Dockerfile; no app or worker service; no healthchecks; default credentials; obsolete `version` key |
| Logging | ⚠️ | `main.py:7`, `handlers.py:7,17` | Duplicated init; f-strings; full user text logged; no correlation IDs; background task logs nothing on failure; no centralization |
| **Overall** | **❌ Not operational** | 8 Python files, ~150 statements | Core request path fails after the first message per worker; no data layer; no tests; secrets in source |

### Readiness against the stated goal

The project can demonstrate "receive a Telegram message and call an LLM" once — after that it breaks (C-3), and it cannot do so at all in a clean environment (C-2) or without exposing a credential (C-1). Of the nine product goals, one is partially present, one is a dead stub, and seven are entirely absent. This is a credible Phase 1 scaffold with a Phase 2 sketch in it, and it is roughly two to three weeks of focused work from a demonstrable AI chat MVP — but that work is entirely Phase 1 hardening and Phase 2 basics. Nothing in Phases 3–9 has been started.

### Explicitly NOT VERIFIED

**NOT VERIFIED — insufficient evidence in repository.** The following could not be determined from the codebase and are stated as unknowns rather than findings:

- Whether the credentials in `.env` and `app/agents/supervisor.py:8` are currently active. No network calls were performed.
- Whether secrets were ever committed. No `.git` directory exists, so there is no history to inspect.
- The intended deployment path. No Dockerfile, CI config, or run documentation exists.
- The exact runtime exception text in C-3. The mechanism is derived from the installed aiogram source at `aiogram/client/session/aiohttp.py:133-146` and `aiogram/client/bot.py:288-293`, but the code was not executed.

---

## 13. What To Build Next

**1. Make one request survive end-to-end, and lock it with a test.**
Fix the five P0s as a single change: move the key into `settings`, add `openai` to `requirements.txt`, rotate the exposed credentials, add `.gitignore`/`.env.example`, replace the per-task event loop in `research_task.py:15-18` with one long-lived loop per worker process, and resolve the HTML/Markdown and 4096-character conflict. Then write the project's first test — call `process_research` twice in the same process and assert both produce a reply. That single test is what proves the core path works, and it would have caught the defect that currently makes the bot unusable.

**2. Add the data tier and real health checks.**
The Postgres container, `DATABASE_URL`, and `asyncpg`/`sqlalchemy` are already provisioned and completely unused. Introduce SQLAlchemy async models plus Alembic for users, conversations, and messages, and make `/health` actually probe Redis, Postgres, and the polling task so the system can report its own state. This unblocks conversation memory and every downstream phase.

**3. Extract a service layer and turn the skeleton into a real chat MVP.**
Stop building `bot` and the LLM client at import time; inject them instead. On top of that, add conversation memory with token budgeting, `/help`, a real error-feedback path, and model selection with a fallback. Only after this is solid is it worth wiring the search/fetch/summarize/cite pipeline into the existing `generate_final_answer` stub — otherwise the research engine will inherit every structural problem now present in the task layer.

---

## Appendix A — Repository Inventory

Complete file listing, excluding `venv/` and `__pycache__/`:

```
.env                          6 lines    secrets: BOT_TOKEN, REDIS_URL, DATABASE_URL, GROQ_API_KEY
.python-version               1 line     3.12.0
docker-compose.yml           23 lines    redis + postgres only
requirements.txt             10 lines    10 pinned deps; openai MISSING; 5 unused
app/__init__.py               0 lines
app/main.py                  24 lines    FastAPI app, /health, polling bootstrap
app/agents/__init__.py        0 lines
app/agents/supervisor.py     65 lines    LLM client (hardcoded key), Supervisor class
app/bot/__init__.py           0 lines
app/bot/dispatcher.py        10 lines    Bot + Dispatcher singletons
app/bot/handlers.py          22 lines    /start + catch-all message handler
app/core/__init__.py          0 lines
app/core/celery_app.py       17 lines    Celery app, broker/backend config
app/core/config.py           12 lines    Settings (4 required vars)
app/tasks/__init__.py         0 lines
app/tasks/research_task.py   18 lines    Celery task, event loop, Telegram send
```

**Totals:** 12 Python files, 7 containing real code, ~150 statements.

**Absent:** `Dockerfile`, `docker-compose` app/worker services, `.gitignore`, `.env.example`, `.dockerignore`, `README`, `CONTRIBUTING`, `Makefile`, `Procfile`, `pyproject.toml`, `alembic.ini`, CI config, any `tests/` directory, any database module, any schema module, any research module.

**Dependency status (`requirements.txt`):**

| Package | Used in `app/`? |
|---|---|
| `fastapi==0.115.6` | ✅ `app/main.py` |
| `uvicorn[standard]==0.34.0` | ⚠️ runtime only, never imported |
| `aiogram==3.17.0` | ✅ `app/bot/` |
| `celery==5.4.0` | ✅ `app/core/celery_app.py`, `app/tasks/` |
| `redis==5.2.0` | ❌ unused (broker accessed via Celery/kombu) |
| `asyncpg==0.30.0` | ❌ unused |
| `sqlalchemy==2.0.36` | ❌ unused |
| `python-dotenv==1.0.1` | ❌ unused (redundant with `pydantic-settings`) |
| `httpx==0.28.1` | ❌ unused |
| `pydantic-settings==2.7.0` | ✅ `app/core/config.py` |
| **`openai`** | **✅ imported at `app/agents/supervisor.py:3` — MISSING from requirements.txt** |

---

## Appendix B — Verified Call Graph

Extracted from the knowledge graph. Total: 80 nodes, 194 edges, 12 Python files, 1 YAML file. Coverage report: `parse_partial: 0`, `skipped: 0`.

### Call edges

```
app.main.on_startup                    ──►  app.main.start_polling
app.tasks.research_task.process_research ──► app.agents.supervisor.Supervisor
app.tasks.research_task.process_research ──► app.agents.supervisor.Supervisor.analyze_query
app.core.config                        ──►  app.core.config.Settings
app.main                               ──►  builtins.dict.get          [health_check]
```

`Supervisor.generate_final_answer` — **0 inbound, 0 outbound. Dead code.**

Note: the edge `research_handler → process_research` is not represented in the graph because `.delay()` is a dynamically resolved attribute call. The edge is confirmed by direct source reading of `app/bot/handlers.py:19-22`.

### Declared dependency edges

```
requirements.txt ──► fastapi, uvicorn, aiogram, celery, redis,
                     asyncpg, sqlalchemy, python-dotenv, httpx, pydantic-settings
```

### Routes

| Method | Path | Handler |
|---|---|---|
| GET | `/health` | `app.main.health_check` |

There are no other HTTP routes. There is no webhook endpoint.

### Clusters detected

| Cluster | Members | Cohesion | Representative nodes |
|---|---|---|---|
| `app` | 3 | 1.0 | `process_research`, `Supervisor`, `analyze_query` |
| `app` | 2 | 1.0 | `on_startup`, `start_polling` |

The graph's own clustering confirms the architecture is two disconnected islands — the Telegram/FastAPI process and the Celery/agent process — joined only by a Redis queue. There is no shared service layer, which is the structural root cause of most findings in section 6.

---

*End of audit. This document was produced under read-only constraints: no files in the application were modified, created, or deleted; no packages were installed; no migrations were run; no configuration or environment variables were changed; no commits were made. The only filesystem change is the creation of this `docs/` directory and this file.*
